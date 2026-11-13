#!/bin/bash
set -e
echo "Serving the AwsIncidentsBench article on port 8080"
exec nginx -g 'daemon off;'
