"""The platform's infrastructure kind, registered when this module is imported.

A study that may meet the kind lists this module in `[study] platforms`, so the kind is known before `study.toml` is
validated and when `check` reads a record that names it. Nothing else lives here: importing the module must be cheap
and have no other effect.
"""

from expcore.errors import register_infra_kind

STORE_KIND = "rdf_store"

# Treatable: a study whose manipulation is the store going away may declare it in infrastructure_is_treatment.
register_infra_kind(STORE_KIND, "the RDF store could not be read")
