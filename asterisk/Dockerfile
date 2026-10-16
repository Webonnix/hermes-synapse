# Hermes telephony edge: Asterisk as a SIP client of the phone provider.
#
# Why Asterisk and not a Python SIP stack: registration to a provider from behind
# NAT, RTP, jitter, codecs and re-INVITEs are exactly the parts that are hard to get
# right and have nothing to do with the product. Asterisk does them; the backend
# drives each call over ARI and receives/sends audio over AudioSocket.
#
# Ubuntu's own asterisk package (universe, maintained by Debian/Canonical) rather
# than a third-party image: see the "trusted publishers only" rule for this
# deployment. 26.04 ships Asterisk 22 with chan_pjsip, res_ari and app/chan_audiosocket.
FROM ubuntu:26.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       asterisk asterisk-modules ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY asterisk/conf/ /etc/asterisk-hermes/
COPY asterisk/entrypoint.sh /usr/local/bin/hermes-asterisk-entrypoint
RUN chmod 0755 /usr/local/bin/hermes-asterisk-entrypoint

# SIP signalling and RTP. Nothing else is published: ARI (8088) is reachable only
# from the backend on the compose network.
EXPOSE 5060/udp 10000-10199/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD asterisk -rx "core show uptime" >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/hermes-asterisk-entrypoint"]
