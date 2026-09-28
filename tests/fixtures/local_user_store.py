from types import SimpleNamespace
from unittest.mock import AsyncMock


def local_user_store(users, *, error=None):
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalars.return_value = SimpleNamespace(all=lambda: users)
    session.scalars.side_effect = error
    return lambda: session
