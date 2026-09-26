import os

from hypothesis import settings

# `ci` searches wider and drops the per-example deadline, which a loaded shared runner can miss;
# `deep` is for the scheduled run. Pick one with HYPOTHESIS_PROFILE.
settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=1_000, deadline=None, print_blob=True)
settings.register_profile("deep", max_examples=20_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
