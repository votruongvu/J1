from temporalio.client import Client

from j1.orchestration.temporal.config import TemporalSettings


async def build_client(settings: TemporalSettings) -> Client:
    return await Client.connect(
        settings.target,
        namespace=settings.namespace,
        tls=settings.tls,
        api_key=settings.api_key,
    )
