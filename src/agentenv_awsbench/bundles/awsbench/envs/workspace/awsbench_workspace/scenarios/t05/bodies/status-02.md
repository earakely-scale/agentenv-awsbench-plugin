# STATUS-02 Status page degraded mode

status.vantleaf.com's publisher stores rendered pages in vaultstore (`vs://status/`). If vaultstore is impaired, status page create/update calls are **accepted and queued but nothing is published**; always verify with the public view.

**Fallback:** write a banner JSON to `s3://vl-statuspage-assets/fallback/banner.json` using `fallback/banner.template.json`. The status.vantleaf.com CloudFront distribution (E1VLSTAT5P3K9) fails over to the S3 origin on 5xx from the vaultstore origin and renders the banner when `active` is true.

- Wording approval: the IC or Sofia Marquez (Peak Freeze §6, Incident Process §4). Public content rules: Incident Process §4.
- Update the banner as status changes; set `"active": false` only after resolution (IC sign-off).
- Queued status page incidents publish when vaultstore recovers.
