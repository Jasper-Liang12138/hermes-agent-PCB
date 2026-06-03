# config-relative-paths Specification

## Purpose
Define how router paths from config.ini are resolved when they are relative, absolute, or environment-variable based.

## Requirements

### Requirement: Relative paths resolved against config.ini location

The system SHALL resolve relative paths in `config.ini` against the directory containing the active `config.ini` file, rather than against the current working directory. Absolute paths and paths expanded from environment variables SHALL be used as-is without base-dir resolution.

#### Scenario: Relative path resolved against config.ini directory

- **WHEN** `config.ini` at `/app/config.ini` contains `arc_dir = ../routers/arc_windows_0519`
- **THEN** `resolve_router_dir("arc")` returns `Path("/routers/arc_windows_0519")`

#### Scenario: Absolute path unchanged

- **WHEN** `config.ini` contains `arc_dir = D:/Routers/arc_windows_0519`
- **THEN** `resolve_router_dir("arc")` returns `Path("D:/Routers/arc_windows_0519")`

#### Scenario: Environment variable in path expanded before resolution

- **WHEN** `config.ini` contains `rl_root_dir = %PROJECT_ROOT%/routers` and `PROJECT_ROOT=D:/Projects`
- **THEN** the path is first expanded to `D:/Projects/routers` (absolute), and used as-is

#### Scenario: PyInstaller bundled config.ini resolution

- **WHEN** running from PyInstaller bundle where `config.ini` is at `sys._MEIPASS/config.ini`
- **THEN** relative paths are resolved against `sys._MEIPASS` directory

#### Scenario: Missing config.ini falls back to current behavior

- **WHEN** no `config.ini` file is found
- **THEN** `resolve_router_dir` falls back to environment variables and CWD, same as before
