# Royal Inn Tartus — website

Static, bilingual (Arabic / English) website of the Royal Inn hotel on the Sea Corniche in Tartus.

- `site/` — the generated pages and assets, served as they are
- `nginx.conf` — clean URLs, security headers, caching
- `Dockerfile` — builds the nginx image; the web fonts are downloaded once at build time and served from the same origin

The site uses no cookies, analytics or third-party requests.
