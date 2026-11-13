# Simulation bench

The data lake can run without a plant. The simulated path is a fleet of sensors whose readings are drawn from a fixed seed and sent the way a flaky network delivers them (out of order, in batches, twice, from several threads at once), the real store on a file or in memory, and the real HTTP server. The checks compare what the data lake returns with an independent model kept by the test. Failures are injected on purpose: a restart, a copy of the database taken while it writes, a file that is not a database, readings that are not numbers, a body that is not JSON, a timestamp from the future and a writer without a token.

## Run it

```
PYTHONPATH=src python -m pytest -q tests/test_simulation_bench.py
```

## What the scenarios check

| Scenario | What it shows |
|----------|---------------|
| batches out of order and retried | the database holds exactly the points of the model; a retry never makes a second point |
| four writers at once | nothing is lost |
| a restart of the service | everything that was acknowledged is still there and the writing goes on |
| a backup taken while the plant writes | the copy is a complete database of its own and knows nothing of what came after it |
| more backups than to keep | the oldest ones are removed first |
| a file that is not a database | it is refused and left as it was |
| readings that are not numbers, a body that is not JSON | a client error and nothing stored |
| a batch with one bad sample | none of the batch is stored |
| a batch or a body over the limit | refused with the right status |
| a timestamp from the future or from the year 1970 | refused |
| a writer without the token, a reader that administers | refused; the reader can still read |
| retention | only what is older than the window goes; applying it again changes nothing |
| the aggregate | the buckets match an average computed by hand |
| the same seed twice, another seed | the run is reproducible and really depends on the seed |

The workflow named "Simulation bench" runs the bench on every change, so the simulated path has its own visible job.

## What it cannot show

How the store behaves on the disk and the power supply of a real controller, with a real SD card or eMMC and a real network, needs the controller. The status of the repository therefore stays at "simulated" for the hardware row until a person has run the service on the bench.
