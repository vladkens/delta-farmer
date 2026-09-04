# Gateway

```bash
cd _gateway
fly apps create delta-gateway
fly redis create --name delta-gateway-redis --region ams --no-replicas
fly redis status delta-gateway-redis
fly secrets set -a delta-gateway ASTRUM_CAPTCHA_KEY='...'
fly secrets set -a delta-gateway REDIS_URL='redis://...'
fly secrets set -a delta-gateway STATUS_IPS='203.0.113.10,203.0.113.11'
fly deploy
```

Copy the Redis `Private URL` printed by `fly redis create` or `fly redis status` into the `REDIS_URL` secret. Obtain `ASTRUM_CAPTCHA_KEY` from the [Astrum dashboard](https://solver.astrum.foundation/). `STATUS_IPS` is a comma-separated list of IPv4 addresses allowed to open the status page.

The service is available at `https://delta-gateway.fly.dev/api`.

The status page is available at `https://delta-gateway.fly.dev/status` from the IP addresses configured in `STATUS_IPS`.
