"""A legacy settings module that mostly matches parity_model, with drift."""

DEBUG = False
SITE_NAME = "app"
ONLY_IN_LEGACY = 2
# The model declares this but withholds a value for it; the deployed base is
# what supplies one, and a value differing from the model's withheld default
# is exactly the case a model-side-only filter would misreport.
WITHHELD_BOUND_IN_LEGACY = "resolved by the legacy module"
