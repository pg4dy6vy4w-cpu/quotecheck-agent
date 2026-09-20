# Pending Apify listing settings

`listing-update.json` is an Actor settings update payload, not run input or a saved Task.
Apply it with an authenticated PUT to `/v2/acts/vic0419~quotecheck-mvp`, then verify the public Actor metadata. It changes title, description, category and exampleRunInput only. No pricing or permissions changes are included.

GitHub builds do not establish that this separate settings update has been applied. The stale exampleRunInput must be checked independently. Do not add credentials to this repository.
