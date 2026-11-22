/// <reference path="../pb_data/types.d.ts" />
// Creates the first superuser from PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD (no-op if unset),
// so the public /_/ installer page is never left open on a fresh deployment.
migrate((app) => {
  const email = $os.getenv("PB_ADMIN_EMAIL")
  const password = $os.getenv("PB_ADMIN_PASSWORD")
  if (!email || !password) {
    return
  }
  const superusers = app.findCollectionByNameOrId("_superusers")
  let record
  try {
    record = app.findAuthRecordByEmail("_superusers", email)
  } catch (_) {
    record = new Record(superusers)
    record.set("email", email)
  }
  record.set("password", password)
  app.save(record)
}, (app) => {
  const email = $os.getenv("PB_ADMIN_EMAIL")
  if (!email) {
    return
  }
  try {
    app.delete(app.findAuthRecordByEmail("_superusers", email))
  } catch (_) {
    // already gone
  }
})
