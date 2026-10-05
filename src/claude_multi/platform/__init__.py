"""POSIX platform seams: Linux and macOS backends.

Import a submodule explicitly. This package imports nothing itself, so a hook
that needs only a lock never loads the service or process backends:

* ``observation`` — dependency-free result types shared by facades and backends;
* ``posix_fs`` — directory fsync, single replace, flock, exec and ownership primitives;
* ``linux_service`` — the service-manager backend (the facade is ``service``);
* ``linux_process`` — bounded ``/proc`` and daemon-location observations;
* ``darwin_process`` — the macOS equivalents through bounded ``lsof``/``ps`` reads;
* ``posix_process`` — process existence, signals and the detached gateway spawn;
* ``file_log`` — per-instance gateway log files and their bounded readers;
* ``mounts`` — the filesystem type under a path and WSL detection.

A missing capability is reported as unknown, never as stopped.
"""
