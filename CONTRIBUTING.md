# Contributing to HYDRA-UMC-DATALAKE 🦾

We welcome contributions to the time-series storage core of the HYDRA-UMC platform.

## Technology Stack
- **Databases**: InfluxDB 2.x, TimescaleDB (PostgreSQL).
- **Languages**: SQL, Flux, Python (for maintenance scripts).
- **Infrastructure**: Docker, Linux (Ubuntu 22.04).

## Guidelines
1. **Schema Design**: All telemetry tags and fields must follow the unified ecosystem naming convention defined in the `schema/` directory.
2. **Query Performance**: Any new retrieval API must be tested against datasets with 10M+ points to ensure sub-second response times.
3. **Data Retention**: Ensure that downsampling and retention policies are correctly implemented to prevent disk exhaustion.
4. **Consistency**: Use transactional writes for critical production logs to ensure data integrity during power failures.
