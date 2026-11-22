/// <reference path="../pb_data/types.d.ts" />
// Per-user metadata mirrored by the FastAPI backend so the frontend can list history
// and subscribe to job progress with the PocketBase SDK.
//
// Trust model: these records are written with the *user's own token*, so they are
// display data only. The backend (files under output/) stays the source of truth for
// credits and job state. Users can sign up freely (default `users` rules); credit
// spending is gated by the backend (verified email + confirm_credit_spend).
migrate((app) => {
  const users = app.findCollectionByNameOrId("users")

  const ownerOnly = "owner = @request.auth.id"
  const createAsOwner = "@request.auth.id != '' && @request.body.owner = @request.auth.id"
  const updateAsOwner = "owner = @request.auth.id && (@request.body.owner:isset = false || @request.body.owner = @request.auth.id)"

  const owner = {
    type: "relation",
    name: "owner",
    required: true,
    collectionId: users.id,
    cascadeDelete: true,
    maxSelect: 1,
  }
  const created = { type: "autodate", name: "created", onCreate: true, onUpdate: false }
  const updated = { type: "autodate", name: "updated", onCreate: true, onUpdate: true }

  const rfqFiles = new Collection({
    type: "base",
    name: "rfq_files",
    listRule: ownerOnly,
    viewRule: ownerOnly,
    createRule: createAsOwner,
    updateRule: updateAsOwner,
    deleteRule: ownerOnly,
    fields: [
      owner,
      { type: "text", name: "upload_id", required: true, max: 64 },
      { type: "text", name: "filename", required: true, max: 200 },
      { type: "number", name: "row_count", onlyInt: true, min: 0 },
      { type: "json", name: "sheets", maxSize: 20000 },
      created,
      updated,
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_rfq_files_upload_id ON rfq_files (upload_id)",
      "CREATE INDEX idx_rfq_files_owner ON rfq_files (owner)",
    ],
  })
  app.save(rfqFiles)

  const scrapeJobs = new Collection({
    type: "base",
    name: "scrape_jobs",
    listRule: ownerOnly,
    viewRule: ownerOnly,
    createRule: createAsOwner,
    updateRule: updateAsOwner,
    deleteRule: null, // history is append-only for users
    fields: [
      owner,
      { type: "text", name: "job_id", required: true, max: 64 },
      {
        type: "select",
        name: "status",
        required: true,
        maxSelect: 1,
        values: ["queued", "running", "completed", "cancelled", "failed"],
      },
      { type: "number", name: "total_families", onlyInt: true, min: 0 },
      { type: "number", name: "completed_families", onlyInt: true, min: 0 },
      { type: "number", name: "credits_spent", onlyInt: true, min: 0 },
      { type: "text", name: "error", max: 2000 },
      created,
      updated,
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_scrape_jobs_job_id ON scrape_jobs (job_id)",
      "CREATE INDEX idx_scrape_jobs_owner ON scrape_jobs (owner)",
    ],
  })
  app.save(scrapeJobs)

  const runs = new Collection({
    type: "base",
    name: "runs",
    listRule: ownerOnly,
    viewRule: ownerOnly,
    createRule: createAsOwner,
    updateRule: updateAsOwner,
    deleteRule: ownerOnly,
    fields: [
      owner,
      { type: "text", name: "kind", required: true, max: 32 },
      { type: "json", name: "summary", maxSize: 2000000 },
      created,
      updated,
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_runs_owner_kind ON runs (owner, kind)",
    ],
  })
  app.save(runs)
}, (app) => {
  for (const name of ["runs", "scrape_jobs", "rfq_files"]) {
    try {
      app.delete(app.findCollectionByNameOrId(name))
    } catch (_) {
      // already gone
    }
  }
})
