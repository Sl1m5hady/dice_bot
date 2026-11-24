#!/bin/sh
set -eu

resolver=$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf)
case "$resolver" in
    "") echo "No container DNS resolver found." >&2; exit 1 ;;
    *:*) resolver="[$resolver]" ;;
esac
printf 'resolver %s valid=5s ipv6=off;\n' "$resolver" > /etc/nginx/conf.d/container-dns.conf

if [ -z "${DICE_DOMAIN:-}" ]; then
    exec nginx -g 'daemon off;'
fi

case "$DICE_DOMAIN" in
    ""|*[!a-z0-9.-]*|.*|*.|*..*|-*|*-) echo "DICE_DOMAIN must be a lowercase domain name." >&2; exit 1 ;;
esac

config=/etc/nginx/conf.d/default.conf
cert="/etc/letsencrypt/live/$DICE_DOMAIN/fullchain.pem"
key="/etc/letsencrypt/live/$DICE_DOMAIN/privkey.pem"

render_http() {
    cat <<EOF
server {
    listen 80;
    server_name $DICE_DOMAIN;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        $1
    }
}
EOF
}

render_https() {
    render_http "return 308 https://$DICE_DOMAIN\$request_uri;"
    cat <<EOF

server {
    listen 443 ssl;
    server_name $DICE_DOMAIN;

    ssl_certificate $cert;
    ssl_certificate_key $key;
    ssl_protocols TLSv1.2 TLSv1.3;

    include /etc/nginx/snippets/dice-proxy.conf;
}
EOF
}

signature() {
    [ -s "$cert" ] && [ -s "$key" ] && sha256sum "$cert" "$key"
}

current=$(signature) || current=""
if [ -n "$current" ]; then
    render_https > "$config"
    if ! nginx -t; then
        current=""
    fi
fi
if [ -z "$current" ]; then
    render_http 'return 503;' > "$config"
fi

(
    while sleep 10; do
        next=$(signature) || continue
        [ "$next" != "$current" ] || continue
        cp "$config" "$config.previous"
        render_https > "$config.next"
        mv "$config.next" "$config"
        if nginx -t && nginx -s reload; then
            current="$next"
            rm "$config.previous"
        else
            mv "$config.previous" "$config"
        fi
    done
) &

exec nginx -g 'daemon off;'
