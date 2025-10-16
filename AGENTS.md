# AGENTS.md

## Purpose
This repository provides a Home Assistant integration for the E7+ electric water heater smart controller (with Wifi extender) by company Secure Meters, running on the Beanbag backend. 


## Key Concepts
The E7+ Secure smart water heater is a dual zone (normal+boost immersion heaters) controller physical device that is installed in the home of a user with a two zone electric immersion water heater physical device. It offers scheduling of normal and boost zones with two separate weekly schedules for on/off time periods Monday to Sunday. The E7+ controller communicates over the user's home Wifi to the company's cloud backend (called Beanbag). A mobile app for Android or iOS can be used to communicate with the cloud backend and control the water heater. The user app does not communicate directly with the E7+ controller or the water heater itself. It only communicates with beanbag, the cloud backend, which in turn communicates over the Internet and the user's Wifi to the E7+ controller. The e7+ then switches the two power relays that turn on and off the immersion heater elements in the water heater. There are no thermostats in the E7+, or any sensor for the water temperature.

## Documentation Map
* `docs/securemtr_openapi.yaml` - Documents the REST login and Websocket startup API for connections to the Beanbag cloud backend. 
* `docs/securemtr_asyncapi.yaml` - Documents the Websocker async API for the Beanbag cloud backend. 
* `docs/architecture.md` — Integration architecture and Python class hierarchy.
* `docs/function_map.txt` - Map of all Python functions in the intefration, with their docstring description. Read and write this for overview of the codebase.

## Usage and Fair Access
* Treat the hosted Beanbag backend as a shared resource; avoid abusive traffic patterns.
* Prefer WebSocket updates once startup completes. Use REST only as a fallback while WebSocket connectivity is unavailable, and apply rate limiting to every REST call, with polite exponential backoff on errors or rate limit messages. 
* Throttle the `import_energy_history` service to a maximum of **2 queries per second**. Importing a year of hourly data can produce thousands of records, so exceeding this rate risks destabilizing the backend. Each user should normally run this import only once.

## Audience Expectations
End users are non-technical Home Assistant operators. Documentation must be task-oriented, step-by-step, and written in clear, plain English to support readers for whom English may be a second language.

## Development Standards
* Follow the Python version declared in `pyproject.toml` and add type hints to all new code.
* Use uv as the package and environment manager
* Provide a concise one-line docstring for every function.
* Apply the **minimal viable change** for each task; avoid touching unrelated code.
* Adhere to DRY principles and practice defensive programming—anticipate invalid input, communication failures, and other error conditions, and handle them gracefully.
* Log major function entry/exit points at `INFO`, protocol interactions at `DEBUG`, and errors at `ERROR`.
* Format and lint all changes with `ruff` before committing.

## Testing Requirements
* Execute `timeout 30s pytest --cov=custom_components.securemtr --cov-report=term-missing`.
* Capture partial logs whenever the timed run aborts; treat timeouts as failures requiring investigation. 
* During debugging, run targeted, no-coverage subsets.
* If tests approach the 30-second limit, suspect an asynchronous wait issue and stop the run rather than letting it hang.
* Test only the code files you have changed. Do not try to fix failing tests unrelated to your changes.
* If you remove duplicate or redundant code, remove the corresponding tests. Do not leave code or tests behind for backwards compatibility. 
* Get 100% coverage on the code you are chaging.
* Write meaningful tests that exercise edge cases, error handling, and invalid inputs, with particular focus on component interfaces.

## Documentation Responsibilities
Document every new feature or behavior change and keep existing documentation in sync.
Add docstrings to all functions
Save all functions and docstrings in docs/function_map.txt

## Pull Request Expectations
* Keep each PR focused on a single feature or test with the minimal supporting changes.
* Summarize the modifications and describe how they were tested.
* Include documentation updates whenever behavior changes.
