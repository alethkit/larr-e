"""SQLite schema for the Pioreactor tables LARR-E reads (a subset of the leader's pioreactor.sqlite).

Used by inject_anomaly.py for rehearsal databases and by the recorder in listener.py.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment TEXT PRIMARY KEY, created_at TEXT NOT NULL, description TEXT,
    media_used TEXT, organism_used TEXT);
CREATE TABLE IF NOT EXISTS od_readings (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    od_reading REAL NOT NULL, angle INTEGER NOT NULL, channel INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS temperature_readings (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    temperature_c REAL NOT NULL);
CREATE TABLE IF NOT EXISTS growth_rates (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    rate REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stirring_rates (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    measured_rpm REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dosing_events (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    event TEXT NOT NULL, volume_change_ml REAL NOT NULL, source_of_event TEXT);
CREATE INDEX IF NOT EXISTS od_readings_ix ON od_readings (experiment, pioreactor_unit, timestamp);
CREATE INDEX IF NOT EXISTS temperature_readings_ix ON temperature_readings (experiment, pioreactor_unit, timestamp);
"""
