"""Web application exports."""

from trpc_service.web.app import create_app
from trpc_service.web.container import ApplicationContainer, build_application_container

__all__ = ["ApplicationContainer", "build_application_container", "create_app"]
