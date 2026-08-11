"""The object key layout, which is a contract and not an implementation detail.

A vendor connector at one deployment reads these objects with code we do not write.
The keys are therefore pinned here rather than left to whatever the store client
happens to produce.
"""

import pytest

from edutap.image_service.objectstore import raw_key, variant_key, version_prefix

UID = "ab12cde@lmu.de"
VERSION = "0198f3c2-7b41-7000-8000-0242ac120002"


def test_the_raw_object_lives_under_its_version():
    assert raw_key(UID, VERSION) == f"{UID}/photo/{VERSION}/raw"


def test_a_variant_carries_its_recipe_in_the_path():
    """The recipe is a level of its own, not folded into the version.

    A changed target size renders a new recipe *beside* the old one, so passes
    pointing at the previous rendering keep working until they are rebuilt. Folding
    it into the version would force a re-render of every stored photograph instead.
    """
    assert variant_key(UID, VERSION, "default", "square-512") == (
        f"{UID}/photo/{VERSION}/default/square-512"
    )


def test_the_version_prefix_covers_the_raw_and_every_rendering():
    """Purging deletes by prefix, so the prefix has to be exactly one version.

    Not `<uid>/photo/` -- that would take every version of the person with it.
    """
    prefix = version_prefix(UID, VERSION)
    assert prefix == f"{UID}/photo/{VERSION}/"
    assert raw_key(UID, VERSION).startswith(prefix)
    assert variant_key(UID, VERSION, "default", "circle-512").startswith(prefix)


def test_the_prefix_ends_with_a_separator():
    """Without it, version `abc` would also match `abcd` and purge a stranger's photo."""
    assert version_prefix(UID, "abc").endswith("/")


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "a/b", "with space", "", "..", "a\\b"],
)
def test_a_component_that_could_escape_the_prefix_is_refused(bad):
    """The person identifier is never interpreted -- but it is still concatenated.

    An identifier carrying a slash or a traversal segment would write outside the
    person's prefix, and this service is the only writer precisely so that cannot
    happen. Refusing here is cheaper than validating at every call site.
    """
    with pytest.raises(ValueError):
        raw_key(bad, VERSION)
    with pytest.raises(ValueError):
        raw_key(UID, bad)


def test_an_ordinary_identifier_with_dots_and_dashes_is_accepted():
    """Dots, at-signs and dashes all occur in real identifiers and none of them escape.

    An ePPN carries the first two, a UUIDv7 the third.
    """
    assert raw_key("a.b-c_d@example.org", "0198-f3c2") is not None
