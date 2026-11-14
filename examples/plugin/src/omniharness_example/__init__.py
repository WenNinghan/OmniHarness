"""A deterministic content check; it does not establish factual correctness."""

from omniharness.plugins import PluginManifest


class RequiredSectionsVerifier:
    manifest = PluginManifest(
        id="example.required-sections",
        name="Required section text",
        version="0.1.0",
        kind="verifier",
        permissions=(),
        side_effect="none",
        config_schema={"type": "object", "additionalProperties": False},
    )

    async def verify(self, artifact: dict, requirements: dict) -> dict:
        content = artifact.get("content")
        sections = requirements.get("required_sections")
        if not isinstance(content, str) or not isinstance(sections, list) or not sections:
            return {
                "status": "UNVERIFIED",
                "message": "Provide text content and a non-empty required_sections list.",
                "checks": [],
            }
        if any(not isinstance(section, str) or not section.strip() for section in sections):
            return {
                "status": "UNVERIFIED",
                "message": "Each required section must be a non-empty string.",
                "checks": [],
            }
        checks = []
        for section in sections:
            start = content.find(section)
            checks.append(
                {
                    "required_text": section,
                    "found": start >= 0,
                    "character_offset": start if start >= 0 else None,
                }
            )
        return {
            "status": "PASS" if all(check["found"] for check in checks) else "FAIL",
            "message": "Exact case-sensitive text presence only; facts and document structure are not verified.",
            "checks": checks,
        }
