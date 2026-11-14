#!/bin/sh
# Grade the exports the harness collected (/export) with the task's own grader; reward = its D12 score.
set -e
exec python3 /tests/run_grade.py --exports /export --out /logs/verifier
