# Gold scanner dashboard

See the project [README](../README.md) for setup and operating limits.

Run npm.cmd ci, then npm.cmd run dev. The development proxy forwards /api
to the local advisory server on port 5000. The server owns the automatic scan
loop; the dashboard polls its latest result once per second.
