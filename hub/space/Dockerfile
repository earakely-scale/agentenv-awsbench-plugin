# AwsIncidentsBench article: an Astro site built from tfrere's research-article-template, served by nginx on port 8080.
FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends nginx && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/package*.json ./
RUN npm ci --no-audit --no-fund

COPY app/ .
# The viewers read /data/index.json and /data/runs/<id>.json, which tools/build_hub_data.py writes to public/data.
RUN npm run build

COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    mkdir -p /var/cache/nginx /var/run /var/log/nginx /var/lib/nginx/body && \
    chmod -R 777 /var/cache/nginx /var/run /var/log/nginx /var/lib/nginx /etc/nginx/nginx.conf /app

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
