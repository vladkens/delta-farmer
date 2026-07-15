# Gateway

```bash
cd _gateway
fly apps create delta-gateway
fly secrets set ASTRUM_CAPTCHA_KEY='...'
fly deploy
```

The service is available at `https://delta-gateway.fly.dev/api`.
