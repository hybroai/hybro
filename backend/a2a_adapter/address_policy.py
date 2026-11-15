"""Which addresses Hybro may connect to on an agent's behalf.

The owner registers an agent at an address; the agent's card then names the
addresses Hybro sends to. The owner is trusted, the card is not:

- A card may point Hybro at a private, loopback, or Docker-host address only
  when the agent was registered at one. The registered address is classified
  by what its host resolves to, not by how it is written, so an internal
  hostname that resolves to a private address counts as private.
- Link-local addresses (``169.254.0.0/16``, ``fe80::/10``) are always refused.
- Public targets are always allowed, so a third-party agent may move between
  public addresses.

Every connection resolves its host here and dials the address that passed the
check (see ``bounded_http.GuardedAsyncTransport``), so DNS rebinding cannot
swap in another address after the check.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from .card_data import build_agent_card
from .constants import SUPPORTED_BINDINGS

REFUSED_DESTINATION_MESSAGE = (
    "The agent card points to a private, loopback, or link-local address. "
    "That is allowed only for an agent registered at a private or loopback "
    "address, and link-local addresses are never allowed."
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


class AddressRefusedError(ValueError):
    """A destination the address policy does not allow."""


_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address, unwrapping IPv4 carried inside IPv6.

    IPv4-mapped (``::ffff:a.b.c.d``) and NAT64 (``64:ff9b::/96``) addresses
    reach the embedded IPv4 address, so they are classified as that address.
    """
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return address.ipv4_mapped
        if address in _NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def address_allowed(value: str, *, allow_private: bool) -> bool:
    """Whether a resolved IP address may be dialed."""
    address = _ip(value)
    if address.is_link_local:
        return False
    return address.is_global or allow_private


async def _getaddrinfo(host: str, port: int) -> list[Any]:
    return await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )


async def resolve_allowed(
    hostname: str, port: int, *, allow_private: bool
) -> list[tuple[int, str]]:
    """Resolve ``hostname`` and return its addresses if every one is allowed.

    One refused answer refuses the host, so a mixed DNS answer cannot steer a
    connection onto the refused address. Resolution errors propagate as
    ``OSError``.
    """
    return _allowed_addresses(
        await _getaddrinfo(hostname, port), allow_private=allow_private
    )


def _allowed_addresses(
    infos: list[Any], *, allow_private: bool
) -> list[tuple[int, str]]:
    addresses: list[tuple[int, str]] = []
    for info in infos:
        family, address = info[0], str(info[4][0])
        if not address_allowed(address, allow_private=allow_private):
            if _ip(address).is_link_local:
                raise AddressRefusedError(
                    "host resolves to a non-public (link-local) address"
                )
            raise AddressRefusedError("host resolves to a non-public address")
        pair = (family, address)
        if pair not in addresses:
            addresses.append(pair)
    if not addresses:
        raise AddressRefusedError("host has no usable address")
    return addresses


def _is_private_answer(infos: list[Any]) -> bool:
    addresses = [_ip(str(info[4][0])) for info in infos]
    return bool(addresses) and not any(address.is_global for address in addresses)


def _host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
    if not parsed.hostname or port is None:
        raise ValueError("URL has no host or supported scheme")
    return parsed.hostname, port


async def registered_address_is_private(registered_url: str | None) -> bool:
    """True when every address the registered host resolves to is non-public.

    A missing, malformed, or unresolvable registration is treated as public,
    so the card cannot gain private access through it.
    """
    return await DestinationPolicy(registered_url).allow_private()


class DestinationPolicy:
    """The destination rule for one call to one agent.

    Use one policy for every request of a call (card fetch, card check, send).
    The registered host is classified once and the decision is kept: when the
    first lookup is of the registered host itself, the answer that is dialed
    is the answer that classifies it. A third party controlling the registered
    host's DNS therefore cannot answer "public" to serve the card and
    "private" to unlock the card's private target.
    """

    def __init__(self, registered_url: str | None) -> None:
        self._registered: tuple[str, int] | None = None
        if registered_url:
            try:
                host, port = _host_port(registered_url)
            except ValueError:
                pass
            else:
                self._registered = (host.lower(), port)
        self._allow_private: bool | None = None

    def _decide(self, allow_private: bool) -> bool:
        if self._allow_private is None:
            self._allow_private = allow_private
        return self._allow_private

    async def allow_private(self) -> bool:
        """Whether this call may reach private and loopback targets.

        When nothing has dialed the registered host yet, this looks it up on
        its own, and that answer is not the one any request dials. The
        rebinding guarantee therefore holds only for a call whose first
        request goes to the registered host (the card fetch).
        """
        if self._allow_private is not None:
            return self._allow_private
        if self._registered is None:
            return self._decide(False)
        try:
            infos = await _getaddrinfo(*self._registered)
        except OSError:
            return self._decide(False)
        return self._decide(_is_private_answer(infos))

    async def resolve(self, hostname: str, port: int) -> list[tuple[int, str]]:
        if (
            self._allow_private is None
            and self._registered is not None
            and hostname.lower() == self._registered[0]
        ):
            infos = await _getaddrinfo(hostname, port)
            allow_private = self._decide(_is_private_answer(infos))
            return _allowed_addresses(infos, allow_private=allow_private)
        return await resolve_allowed(
            hostname, port, allow_private=await self.allow_private()
        )


async def ensure_card_destinations_allowed(
    card_data: Any, *, policy: DestinationPolicy
) -> None:
    """Refuse a card whose callable interfaces the policy does not allow.

    Checked before the first send so a refused card fails with a clear reason
    instead of a transport error. Interfaces on bindings Hybro never calls are
    ignored. The connect-time check in the transport still applies.
    """
    card = build_agent_card(card_data)
    supported = {binding.upper() for binding in SUPPORTED_BINDINGS}
    for interface in card.supported_interfaces:
        binding = str(interface.protocol_binding or "").upper()
        if binding and binding not in supported:
            continue
        try:
            host, port = _host_port(interface.url)
            await policy.resolve(host, port)
        except AddressRefusedError:
            raise AddressRefusedError(REFUSED_DESTINATION_MESSAGE) from None
        except (OSError, ValueError):
            # Unresolvable or malformed: the send fails on its own.
            continue


__all__ = [
    "REFUSED_DESTINATION_MESSAGE",
    "AddressRefusedError",
    "DestinationPolicy",
    "address_allowed",
    "ensure_card_destinations_allowed",
    "registered_address_is_private",
    "resolve_allowed",
]
