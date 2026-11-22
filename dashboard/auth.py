import base64
import binascii
import json
from dataclasses import dataclass, field, replace
from uuid import UUID

OPERATOR = "ReleaseOperator"
APPROVER = "ReleaseApprover"
ROLES = frozenset({OPERATOR, APPROVER})
MS = "http://schemas.microsoft.com/identity/claims/"
SOAP = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/"


class Denied(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    object_id: str
    roles: frozenset[str]
    display_name: str = field(default="", compare=False)


def actor_from_principal(header: str | None, tenant_id: str) -> Actor:
    if not header or len(header) > 32768:
        raise Denied("Sign in with Microsoft Entra")
    try:
        value = json.loads(base64.b64decode(header, validate=True))
        claims = {}
        for item in value["claims"]:
            claims.setdefault(item["typ"], set()).add(item["val"])

        def claim(*names):
            values = set().union(*(claims.get(name, set()) for name in names))
            if len(values) > 1:
                raise ValueError
            return next(iter(values), "")

        tenant = str(UUID(claim("tid", MS + "tenantid")))
        oid = str(UUID(claim("oid", MS + "objectidentifier")))
        roles = ROLES.intersection(set().union(*(claims.get(t, set()) for t in
                                                ("roles", "role", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"))))
        kind = claim("idtyp", MS + "idtyp")
        # Easy Auth can also authenticate client applications. Require a user claim
        # when the Entra token does not carry an explicit identity type.
        has_user_name = any(claims.get(name) for name in ("preferred_username", "upn", SOAP + "upn"))
        if tenant != tenant_id or not roles or (kind != "user" and (kind or not has_user_name)):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise Denied("An assigned Entra user role is required") from None
    return Actor(tenant, oid, roles)


def authorize(actor: Actor, role: str) -> None:
    if role not in actor.roles:
        raise Denied("Your role does not allow this action")


def verify_write_origin(headers, origin: str) -> None:
    if headers.get("origin") != origin or headers.get("x-dashboard-write") != "1":
        raise Denied("Use the management console to perform this action")


class EntraSession:
    def __init__(self, tenant_id, audience):
        from jwt import PyJWKClient

        self.tenant_id, self.audience = tenant_id, audience
        self.issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self.keys = PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys", timeout=5)

    def __call__(self, token):
        import jwt

        if not token or len(token) > 32768:
            raise Denied("Sign in with Microsoft Entra")
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                                options={"require": ["iss", "aud", "exp", "nbf", "iat", "tid", "oid"],
                                         "strict_aud": True}, leeway=30)
            if claims.get("scp") or not isinstance(claims.get("roles"), list):
                raise ValueError
            actor = actor_from_principal(base64.b64encode(json.dumps({"claims": [
                {"typ": name, "val": value} for name, value in claims.items()
                if name in ("tid", "oid", "idtyp", "preferred_username", "upn")
            ] + [{"typ": "roles", "val": role} for role in claims["roles"]]}).encode()).decode(), self.tenant_id)
            name = next((value.strip() for key in ("name", "preferred_username", "upn")
                         if isinstance(value := claims.get(key), str) and value.strip()), "")
            return replace(actor, display_name=name)
        except (jwt.PyJWTError, ValueError, TypeError):
            raise Denied("A valid Entra session is required") from None
