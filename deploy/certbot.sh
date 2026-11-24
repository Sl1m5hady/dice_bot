#!/bin/sh
set -eu

cert="/etc/letsencrypt/live/$DICE_DOMAIN/fullchain.pem"
if [ ! -s "$cert" ]; then
    until certbot certonly --non-interactive --agree-tos --keep-until-expiring \
        --webroot -w /var/www/certbot --cert-name "$DICE_DOMAIN" \
        --domain "$DICE_DOMAIN" --email "$CERTBOT_EMAIL"; do
        echo "Certificate issuance failed. Retrying in 15 minutes." >&2
        sleep 900
    done
fi

while :; do
    certbot renew --quiet --cert-name "$DICE_DOMAIN" || echo "Certificate renewal check failed." >&2
    sleep 43200
done
