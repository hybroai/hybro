"""Setup verification uses the same adapters, with all provider traffic mocked."""


def test_production_catalog_offers_optional_images_only_for_eligible_provider():
    from common.config.runtime_config import RuntimeProvider
    from llm_gateway.catalog import model_choices

    for provider in ("openai", "deepseek", "anthropic"):
        expected = (
            ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
            if provider == "openai"
            else ()
        )
        assert (
            model_choices(RuntimeProvider(id=provider, auth="api_key")).image
            == expected
        )
