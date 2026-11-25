"""Every model module must be imported in app/models/__init__.py so Alembic sees it."""

import importlib
import pkgutil
from pathlib import Path

from app import models
from app.db.base import Base

NON_MODEL_MODULES = {"enums"}


def test_every_model_module_is_registered() -> None:
    package_dir = Path(models.__file__).parent
    module_names = {m.name for m in pkgutil.iter_modules([str(package_dir)])}
    model_modules = module_names - NON_MODEL_MODULES
    assert model_modules, "no model modules found"

    registered = set(models.__all__)
    for name in sorted(model_modules):
        module = importlib.import_module(f"app.models.{name}")
        classes = {
            obj.__name__
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, Base)
            and obj is not Base
            and obj.__module__ == module.__name__
        }
        assert classes, f"{name} defines no model"
        assert classes <= registered, f"{name} is not registered in app/models/__init__.py"


def test_new_tables_are_in_metadata() -> None:
    assert {"api_calls", "analysis_runs", "article_analyses"} <= set(Base.metadata.tables)
