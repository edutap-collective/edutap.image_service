"""Which derivatives a version gets rendered into.

Configuration rather than code. A different institution needs different sizes, and
a service that hard-codes one set is not a standard anybody else can adopt — which
is the whole reason this package sits in the collective rather than at one site.

The recipe is a level of its own in the object key, so changing the sizes means
adding a manifest *beside* the current one. Renderings of both then coexist while
issued passes catch up, instead of every stored photograph having to be rebuilt at
once.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    """One rendering: what to ask `edutap.image_api` for, and what to keep.

    `to_jpeg` exists because `/crop/` has no format parameter and always answers
    PNG. A masked variant has to stay PNG for its alpha channel; an unmasked
    portrait as PNG is roughly six times the bytes of the same picture as JPEG,
    per stored version, per person. Re-encoding is the cheaper of the two evils
    until the other service can emit a format.
    """

    name: str
    mask: str = "none"
    aspect_ratio: str = "square"
    height: int = 512
    width: int | str = "auto"
    to_jpeg: bool = False

    @property
    def content_type(self) -> str:
        """What the object is stored and served as."""
        return "image/jpeg" if self.to_jpeg else "image/png"


@dataclass(frozen=True)
class Manifest:
    """A named set of renderings."""

    name: str
    variants: tuple[Variant, ...]


#: The provisional default.
#:
#: A square portrait in two sizes and one circular cut, which is what a card and a
#: wallet pass need between them. It is explicitly **not** derived from the wallet
#: asset sizes `edutap.image_api` offers: those are pass artwork -- logo, hero,
#: strip -- and contain no portrait slot beyond Apple's 90x90 thumbnail. A person's
#: photograph is a `/crop/` product, not a wallet asset.
#:
#: The real values follow from what the pass templates bind, which is not yet
#: settled. Adding them is a new manifest beside this one, not an edit of it.
DEFAULT = Manifest(
    name="default",
    variants=(
        Variant(name="square-512", height=512, width=512, to_jpeg=True),
        Variant(name="square-1024", height=1024, width=1024, to_jpeg=True),
        Variant(name="circle-512", mask="circle", height=512, width=512),
    ),
)

MANIFESTS = {DEFAULT.name: DEFAULT}


def manifest(name: str) -> Manifest:
    """Return the named manifest, or fail at the point of configuration."""
    try:
        return MANIFESTS[name]
    except KeyError:
        raise LookupError(f"no manifest named {name!r}") from None
