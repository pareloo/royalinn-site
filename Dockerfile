# Royal Inn Tartus — static site served by nginx

# 1) Web fonts (El Messiri, Almarai, Cormorant Garamond — SIL Open Font License) are fetched once, while the image
#    is built, and then served from this site. Visitors never contact a third party.
FROM alpine:3.20 AS fonts
RUN apk add --no-cache wget ca-certificates
WORKDIR /f
RUN set -eu; \
    wget -q -U "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36" \
      -O fonts-g.css "https://fonts.googleapis.com/css2?family=Almarai:wght@400;700&family=Cormorant+Garamond:wght@400;500;600&family=El+Messiri:wght@500;600;700&display=swap"; \
    mkdir g; \
    for u in $(grep -o 'https://fonts.gstatic.com/[^)]*' fonts-g.css | sort -u); do \
      n=$(printf '%s' "$u" | sed 's#https://fonts.gstatic.com/##; s#[^A-Za-z0-9._-]#-#g'); \
      wget -q -O "g/$n" "$u"; \
      sed -i "s#$u#/assets/fonts/g/$n#g" fonts-g.css; \
    done; \
    test "$(ls g | wc -l)" -ge 3; \
    if grep -q 'gstatic' fonts-g.css; then echo "font urls not rewritten"; exit 1; fi; \
    grep -q "El Messiri" fonts-g.css; grep -q "Almarai" fonts-g.css; grep -q "Cormorant Garamond" fonts-g.css; \
    ls -la g | head -40

# 2) The site
FROM nginx:stable-alpine
RUN apk add --no-cache python3 && rm -f /etc/nginx/conf.d/default.conf && mkdir -p /data /app
COPY nginx.conf /etc/nginx/conf.d/royalinn.conf
COPY site/ /usr/share/nginx/html/
COPY --from=fonts /f/g/ /usr/share/nginx/html/assets/fonts/g/
COPY --from=fonts /f/fonts-g.css /usr/share/nginx/html/assets/css/fonts-g.css
COPY app/api.py /app/api.py
COPY start.sh /start.sh
RUN chmod 755 /start.sh && python3 -m py_compile /app/api.py
VOLUME /data
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD wget -q --spider http://127.0.0.1/robots.txt && wget -q --spider http://127.0.0.1/api/health || exit 1
CMD ["/start.sh"]
