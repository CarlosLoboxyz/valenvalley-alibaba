/// <reference path="../pb_data/types.d.ts" />
// Application URL and optional SMTP, read from the environment. SMTP is REQUIRED for
// email verification; the backend refuses credit-spending calls from unverified users
// unless REQUIRE_VERIFIED_EMAIL=false.
migrate((app) => {
  const settings = app.settings()

  const appName = $os.getenv("PB_APP_NAME")
  const appURL = $os.getenv("PB_APP_URL")
  if (appName) settings.meta.appName = appName
  if (appURL) settings.meta.appURL = appURL

  const host = $os.getenv("PB_SMTP_HOST")
  if (host) {
    settings.smtp.enabled = true
    settings.smtp.host = host
    settings.smtp.port = parseInt($os.getenv("PB_SMTP_PORT") || "587", 10)
    settings.smtp.username = $os.getenv("PB_SMTP_USERNAME")
    settings.smtp.password = $os.getenv("PB_SMTP_PASSWORD")
    settings.smtp.tls = ($os.getenv("PB_SMTP_TLS") || "false") === "true"
    const sender = $os.getenv("PB_SMTP_SENDER")
    if (sender) settings.meta.senderAddress = sender
  }

  app.save(settings)
}, (app) => {})
