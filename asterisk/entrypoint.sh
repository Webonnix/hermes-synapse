#!/bin/sh
# Renders the Hermes Asterisk configuration and starts Asterisk in the foreground.
#
# State shared with the backend lives in $HERMES_ASTERISK_STATE (a bind mount of
# data/asterisk on the host, also visible to the backend under its data dir):
#   ari.secret           ARI password, generated here on first start
#   pjsip_trunks.conf    provider trunks, written by backend/phone_lines.py
#   public_ip            optional external address for SDP/Contact behind NAT
set -eu

STATE="${HERMES_ASTERISK_STATE:-/var/lib/hermes-asterisk}"
mkdir -p "$STATE"
umask 077

if [ ! -s "$STATE/ari.secret" ]; then
    head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40 > "$STATE/ari.secret"
fi
[ -f "$STATE/pjsip_trunks.conf" ] || : > "$STATE/pjsip_trunks.conf"

ARI_SECRET="$(cat "$STATE/ari.secret")"

cp /etc/asterisk-hermes/*.conf /etc/asterisk/

sed -i "s|@ARI_SECRET@|${ARI_SECRET}|" /etc/asterisk/ari.conf
sed -i "s|@STATE@|${STATE}|g" /etc/asterisk/pjsip.conf

PUBLIC_IP="${HERMES_PUBLIC_IP:-}"
if [ -z "$PUBLIC_IP" ] && [ -s "$STATE/public_ip" ]; then
    PUBLIC_IP="$(tr -dc '0-9.' < "$STATE/public_ip")"
fi
if [ -n "$PUBLIC_IP" ]; then
    # Only with a known external address: local_net without one is a no-op, and a
    # wrong one breaks audio in exactly the calls it was meant to fix.
    sed -i "s|^;@EXTERNAL@|external_media_address=${PUBLIC_IP}\nexternal_signaling_address=${PUBLIC_IP}|" /etc/asterisk/pjsip.conf
fi

exec asterisk -f -g -vvv
