"""Drive a user-authenticated Gemini web session on the VPS VNC desktop."""
import json
import os
import sys
import time
import urllib.request
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # The extraction helpers are also tested on Windows.
    fcntl = None


os.environ.setdefault('NODE_NO_WARNINGS', '1')
CDP_URL = os.environ.get('GEMINI_CDP_URL', 'http://127.0.0.1:9223')
GEMINI_URL = 'https://gemini.google.com/app'
LOCK_PATH = os.environ.get('GEMINI_WORKER_LOCK', '/tmp/tradew-gemini-review.lock')
INPUT_SELECTORS = (
    'rich-textarea .ql-editor[contenteditable="true"]',
    'rich-textarea [contenteditable="true"]',
    'textarea[aria-label*="prompt" i]',
    '[contenteditable="true"][role="textbox"]',
)
RESPONSE_SELECTORS = (
    'model-response .markdown-main-panel',
    'model-response message-content',
    '[data-message-author-role="model"]',
    'model-response',
)
SEND_SELECTORS = (
    'button.send-button',
    'button[aria-label*="Send message" i]',
    'button[aria-label^="Send" i]',
    'button[data-test-id*="send" i]',
)
STOP_SELECTORS = (
    'button[aria-label*="Stop response" i]',
    'button[aria-label*="Stop generating" i]',
    'button.stop-button',
)
AUTOMATION_PAGE_NAME = 'tradew-gemini-automation'
SIGN_IN_SELECTORS = (
    'a[href*="accounts.google.com"]:has-text("Sign in")',
    'button:has-text("Sign in")',
    'a:has-text("Sign in")',
)


def _browser_endpoint_ready():
    try:
        with urllib.request.urlopen(CDP_URL + '/json/version', timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _visible(page, selectors):
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count() - 1, -1, -1):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
    return None


def _gemini_page(browser, create=False):
    pages = [page for context in browser.contexts for page in context.pages]
    for page in pages:
        if 'gemini.google.com' in page.url:
            return page
    if not create:
        return None
    if not browser.contexts:
        raise RuntimeError('The VNC Chrome profile has no browser context')
    page = browser.contexts[0].new_page()
    page.goto(GEMINI_URL, wait_until='domcontentloaded', timeout=30000)
    return page


def _automation_page(browser):
    """Reuse one dedicated tab without touching the user's VNC Gemini tab."""
    if not browser.contexts:
        raise RuntimeError('The VNC Chrome profile has no browser context')
    context = browser.contexts[0]
    matches = []
    for candidate in context.pages:
        try:
            if candidate.evaluate('window.name') == AUTOMATION_PAGE_NAME:
                matches.append(candidate)
        except Exception:
            continue
    page = matches[0] if matches else context.new_page()
    for duplicate in matches[1:]:
        try:
            duplicate.close()
        except Exception:
            pass
    try:
        page.evaluate('(name) => { window.name = name; }', AUTOMATION_PAGE_NAME)
    except Exception:
        pass
    return page


@contextmanager
def _exclusive_review():
    """Prevent two worker processes from typing into the browser concurrently."""
    lock = open(LOCK_PATH, 'a+', encoding='utf-8')
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('A Gemini browser review is already in progress') from exc
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _response_locator(page):
    for selector in RESPONSE_SELECTORS:
        locator = page.locator(selector)
        if locator.count():
            return locator
    return page.locator(RESPONSE_SELECTORS[-1])


def _extract_json(text):
    cleaned = text.strip()
    if cleaned.startswith('```'):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        cleaned = '\n'.join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char != '{':
                continue
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    raise RuntimeError('Gemini web reply did not contain a valid JSON object')


def _complete_json(text):
    """Return a completed JSON object, or None while Gemini is still streaming it."""
    if not text:
        return None
    try:
        return _extract_json(text)
    except RuntimeError:
        return None


def _status():
    result = {'ready': False, 'browser_connected': _browser_endpoint_ready(),
              'authenticated': False, 'transport': 'vnc_browser'}
    if not result['browser_connected']:
        return result
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(CDP_URL)
            page = _gemini_page(browser)
            result['gemini_page_count'] = sum(
                1 for context in browser.contexts for candidate in context.pages
                if 'gemini.google.com' in candidate.url)
            result['gemini_page_open'] = page is not None
            if page:
                result['prompt_available'] = _visible(page, INPUT_SELECTORS) is not None
                result['sign_in_visible'] = _visible(page, SIGN_IN_SELECTORS) is not None
                result['authenticated'] = (result['prompt_available'] and
                                           not result['sign_in_visible'])
            result['ready'] = result['authenticated']
    except Exception as exc:
        result['error'] = str(exc).replace('\n', ' ')[-300:]
    return result


def _review_once(request):
    from playwright.sync_api import sync_playwright
    timeout_seconds = min(max(float(request.get('timeout_seconds', 90)), 10), 600)
    started = time.monotonic()
    timing = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError('The VNC Chrome profile has no browser context')
        # Never navigate or type into the user's long-lived VNC tab. Reuse one
        # dedicated automation tab to avoid a costly new renderer per review.
        page = _automation_page(browser)
        try:
            page.goto(GEMINI_URL, wait_until='domcontentloaded', timeout=30000)
            try:
                page.evaluate('(name) => { window.name = name; }', AUTOMATION_PAGE_NAME)
            except Exception:
                pass
            timing['navigation_seconds'] = round(time.monotonic() - started, 3)
            prompt_box = None
            prompt_deadline = time.monotonic() + 30
            while time.monotonic() < prompt_deadline:
                prompt_box = _visible(page, INPUT_SELECTORS)
                if prompt_box is not None:
                    break
                page.wait_for_timeout(500)
            if prompt_box is None:
                raise RuntimeError('Gemini is not signed in. Complete Google sign-in in the VNC Chrome window.')
            timing['prompt_ready_seconds'] = round(time.monotonic() - started, 3)
            if _visible(page, SIGN_IN_SELECTORS) is not None:
                raise RuntimeError('Gemini is open but Google sign-in is incomplete in the VNC Chrome window.')
            responses = _response_locator(page)
            before_count = responses.count()
            before_text = responses.nth(before_count - 1).inner_text() if before_count else ''
            prompt_box.click()
            prompt_box.fill(request['prompt'])
            send = None
            send_deadline = time.monotonic() + 15
            while time.monotonic() < send_deadline:
                send = _visible(page, SEND_SELECTORS)
                if send is not None and send.is_enabled():
                    break
                page.wait_for_timeout(250)
            if send is not None and send.is_enabled():
                send.click()
            else:
                prompt_box.press('Enter')
            timing['prompt_sent_seconds'] = round(time.monotonic() - started, 3)

            # Do not consume the entire model timeout when the evolving web UI
            # did not actually submit the prompt.
            submit_deadline = time.monotonic() + 15
            submitted = False
            while time.monotonic() < submit_deadline:
                responses = _response_locator(page)
                count = responses.count()
                if (count > before_count or _visible(page, STOP_SELECTORS) is not None or
                        page.url.rstrip('/') != GEMINI_URL.rstrip('/')):
                    submitted = True
                    break
                page.wait_for_timeout(250)
            if not submitted:
                raise RuntimeError(
                    'Gemini prompt did not submit; its web composer remained idle')

            last_text = ''
            stable_since = None
            response_count = before_count
            # The configured timeout is a response budget. Browser navigation
            # and composer discovery must not silently consume most of it.
            response_deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < response_deadline:
                page.wait_for_timeout(500)
                responses = _response_locator(page)
                count = responses.count()
                response_count = count
                text = responses.nth(count - 1).inner_text().strip() if count else ''
                changed = count > before_count or (text and text != before_text)
                if not changed:
                    continue
                if text != last_text:
                    last_text = text
                    stable_since = time.monotonic()
                elif last_text and stable_since and time.monotonic() - stable_since >= 2:
                    # Gemini sometimes leaves its stop control visible after the
                    # response DOM already contains complete JSON. Valid JSON is a
                    # stronger completion signal than that transient UI control.
                    completed = _complete_json(last_text)
                    if completed is not None:
                        return completed
                    if (_visible(page, STOP_SELECTORS) is None and
                            time.monotonic()-stable_since >= 5):
                        excerpt = ' '.join(last_text.split())[-180:]
                        raise RuntimeError(
                            'Gemini completed a non-JSON reply: '+excerpt)
            completed = _complete_json(last_text)
            if completed is not None:
                return completed
            stop_visible = _visible(page, STOP_SELECTORS) is not None
            if last_text:
                raise RuntimeError(
                    'Gemini reply did not become valid JSON before timeout '
                    f'(response_nodes={response_count}, response_chars={len(last_text)}, '
                    f'stop_visible={stop_visible})')
            raise TimeoutError(
                'Timed out without a detectable Gemini response '
                f'(response_nodes={response_count}, stop_visible={stop_visible}, '
                f'url={page.url})')
        finally:
            timing['total_seconds'] = round(time.monotonic() - started, 3)
            print('GEMINI_WORKER_TIMING ' + json.dumps(timing), file=sys.stderr)
            if not page.is_closed():
                stop = _visible(page, STOP_SELECTORS)
                if stop is not None and stop.is_enabled():
                    stop.click()
                    page.wait_for_timeout(250)


def _transient_gemini_failure(exc):
    """Retry only Gemini's own generic transport/service failure pages."""
    message = str(exc).lower()
    return any(fragment in message for fragment in (
        'sorry, something went wrong',
        'something went wrong. please try your request again',
        'an unexpected error has occurred',
    ))


def _discard_automation_page():
    """Close the failed renderer so a retry starts in a genuinely fresh tab."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        for context in browser.contexts:
            for page in list(context.pages):
                try:
                    if page.evaluate('window.name') == AUTOMATION_PAGE_NAME:
                        page.close()
                except Exception:
                    continue


def _review(request):
    """Run once normally and make one bounded retry for a Gemini service error."""
    try:
        return _review_once(request)
    except Exception as exc:
        if not _transient_gemini_failure(exc):
            raise
        print('GEMINI_WORKER_RETRY transient_service_error', file=sys.stderr)
        _discard_automation_page()
        time.sleep(2)
        return _review_once(request)


def main():
    if '--status' in sys.argv:
        print(json.dumps(_status()))
        return 0
    if not _browser_endpoint_ready():
        raise RuntimeError('The VNC Gemini Chrome service is not reachable')
    request = json.load(sys.stdin)
    with _exclusive_review():
        print(json.dumps(_review(request), allow_nan=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc).replace('\n', ' ')[-500:], file=sys.stderr)
        raise SystemExit(1)
