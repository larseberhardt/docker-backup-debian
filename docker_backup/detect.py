"""DB detection from the image and credential extraction from the environment.

Pure functions — testable without Docker (against a captured compose-config JSON).
"""

from __future__ import annotations

import re

from typing import Any, Dict, List, Optional

# Image tokens → engine. Matched as a standalone word (see _has_token), not as
# a substring — otherwise PostgREST ("postgrest") would be detected as Postgres.
# "postgresql" is its own token because "postgres" does not match inside
# "postgresql" due to the word boundary (e.g. bitnami/postgresql).
_MYSQL_TOKENS = ("mariadb", "mysql", "percona")
_POSTGRES_TOKENS = ("postgis", "postgresql", "postgres")

# Sidecars/tools whose image name contains an engine token but which are NOT a
# database (e.g. supabase/postgres-meta, prodrigestivill/postgres-backup-local,
# wrouesnel/postgres_exporter). Would otherwise be wrongly detected as a DB and dumped.
_NOT_DB_MARKERS = (
    "meta", "backup", "exporter", "admin", "proxy", "operator",
    "migrate", "manager", "metrics", "dump",
)

# Supabase bundles several databases (postgres + _supabase) and uses a
# superuser (supabase_admin) for full dumps including roles/privileges.
_SUPABASE_INTERNAL_DB = "_supabase"


def _image_name(image: Optional[str]) -> str:
    # registry/repo:tag@digest → registry/repo
    if not image:
        return ""
    return image.lower().split("@", 1)[0].split(":", 1)[0]


def _has_token(base: str, token: str) -> bool:
    """``token`` as a standalone word in ``base`` — with no directly adjacent
    letters. So "postgres" matches in "postgres-meta" and "postgres12", but not
    in "postgrest" (PostgREST is not a database)."""
    return re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(token), base) is not None


def _image_engine(image: Optional[str]) -> Optional[str]:
    name = _image_name(image)
    if not name:
        return None
    base = name.rsplit("/", 1)[-1]
    if any(marker in base for marker in _NOT_DB_MARKERS):
        return None  # sidecar/tool, not a DB (e.g. postgres-meta, postgres-backup-local)
    for token in _MYSQL_TOKENS:
        if _has_token(base, token):
            return "mysql"
    for token in _POSTGRES_TOKENS:
        if _has_token(base, token):
            return "postgres"
    return None


def _image_flavor(image: Optional[str]) -> Optional[str]:
    """Detects distributions that need special handling. Currently only Supabase."""
    name = _image_name(image)
    if "supabase/postgres" in name or name.split("/", 1)[0] == "supabase":
        return "supabase"
    return None


# Stateful engines WITHOUT dump support here: their data is captured only as a live
# file copy (crash-consistent at best; MongoDB in particular may be unusable without
# a mongodump pre-hook). Matched like the DB tokens (word boundary, sidecars skipped).
# mongo/redis appear here too, but ``create`` suppresses the warning for services
# covered by the built-in quiesce (see find_quiesce_services).
_STATEFUL_TOKENS = (
    ("mongodb", "MongoDB"), ("mongo", "MongoDB"),
    ("redis", "Redis"), ("valkey", "Valkey"), ("keydb", "KeyDB"),
    ("elasticsearch", "Elasticsearch"), ("opensearch", "OpenSearch"),
    ("clickhouse", "ClickHouse"), ("cassandra", "Cassandra"), ("scylladb", "ScyllaDB"),
    ("couchdb", "CouchDB"), ("influxdb", "InfluxDB"), ("neo4j", "Neo4j"),
    ("rabbitmq", "RabbitMQ"), ("etcd", "etcd"),
)

# Engines the built-in quiesce can capture consistently WITHOUT a dump:
# mongo → db.fsyncLock() around the file capture; redis family → BGSAVE checkpoint.
_QUIESCE_TOKENS = (
    ("mongodb", "mongo"), ("mongo", "mongo"),
    ("redis", "redis"), ("valkey", "redis"), ("keydb", "redis"),
)


def find_undumpable_stateful(compose_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Services with a known stateful engine that has NO logical dump support.

    Used by ``create`` to warn that these are backed up as a file copy only
    (crash-consistent) and that a pre-backup dump hook is recommended.
    """
    out = []  # type: List[Dict[str, Any]]
    for svc_name, svc in (compose_json.get("services") or {}).items():
        image = svc.get("image")
        name = _image_name(image)
        if not name:
            continue
        base = name.rsplit("/", 1)[-1]
        if any(marker in base for marker in _NOT_DB_MARKERS):
            continue  # exporter/admin/proxy sidecar, not the datastore itself
        if _image_engine(image):
            continue  # mysql/postgres → covered by a logical dump
        for token, label in _STATEFUL_TOKENS:
            if _has_token(base, token):
                out.append({"service": svc_name, "engine": label, "image": image})
                break
    return out


def find_db_services(compose_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Returns all DB services (several per stack are possible)."""
    result = []  # type: List[Dict[str, Any]]
    for svc_name, svc in (compose_json.get("services") or {}).items():
        engine = _image_engine(svc.get("image"))
        if engine:
            result.append({
                "service": svc_name,
                "engine": engine,
                "image": svc.get("image"),
                "flavor": _image_flavor(svc.get("image")),
                "environment": svc.get("environment") or {},
            })
    return result


def find_quiesce_services(compose_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Services the built-in quiesce handles (mongo: fsyncLock, redis: BGSAVE).

    Returns per service: name, engine ('mongo'|'redis'), environment and the
    volume list (the latter decides how long the lock must be held)."""
    out = []  # type: List[Dict[str, Any]]
    for svc_name, svc in (compose_json.get("services") or {}).items():
        image = svc.get("image")
        name = _image_name(image)
        if not name:
            continue
        base = name.rsplit("/", 1)[-1]
        if any(marker in base for marker in _NOT_DB_MARKERS):
            continue  # exporter/admin sidecar, not the datastore
        if _image_engine(image):
            continue  # mysql/postgres → logical dump instead
        for token, engine in _QUIESCE_TOKENS:
            if _has_token(base, token):
                out.append({
                    "service": svc_name,
                    "engine": engine,
                    "environment": svc.get("environment") or {},
                    "volumes": svc.get("volumes") or [],
                })
                break
    return out


def extract_quiesce_credentials(
    env: Optional[Dict[str, str]], engine: str
) -> Dict[str, Any]:
    """Auth source for the quiesce commands, from the service environment.

    Like the DB dumps, only the ENV KEY is persisted (resolved freshly at run
    time → picks up rotations); ``user_value`` is a literal (bitnami: 'root').
    All fields may be None — most compose Mongo/Redis setups run without auth
    on the internal network."""
    env = env or {}
    if engine == "mongo":
        if env.get("MONGO_INITDB_ROOT_USERNAME") and env.get("MONGO_INITDB_ROOT_PASSWORD"):
            return {"user_value": None, "user_env_key": "MONGO_INITDB_ROOT_USERNAME",
                    "password_env_key": "MONGO_INITDB_ROOT_PASSWORD"}
        if env.get("MONGODB_ROOT_PASSWORD"):  # bitnami
            return {"user_value": env.get("MONGODB_ROOT_USER") or "root",
                    "user_env_key": None, "password_env_key": "MONGODB_ROOT_PASSWORD"}
    elif engine == "redis":
        if env.get("REDIS_PASSWORD"):  # bitnami; official image: auth not detectable
            return {"user_value": None, "user_env_key": None,
                    "password_env_key": "REDIS_PASSWORD"}
    return {"user_value": None, "user_env_key": None, "password_env_key": None}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def extract_credentials(
    env: Optional[Dict[str, str]], engine: str, flavor: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Extracts user/password source/DB from the resolved service environment.

    Return fields: engine, user, password, password_env_key, all_databases,
    databases, dump_globals, source ('root'|'app'|'postgres'|'supabase'|'unknown').
    ``flavor`` ('supabase') enables multi-DB dumps + cluster globals (roles).
    The password itself is only returned here for the interactive wizard;
    ``password_env_key`` is persisted instead (see runtime.resolve_password).
    """
    env = env or {}

    if engine == "mysql":
        root_pw = env.get("MYSQL_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD")
        root_key = "MYSQL_ROOT_PASSWORD" if env.get("MYSQL_ROOT_PASSWORD") else (
            "MARIADB_ROOT_PASSWORD" if env.get("MARIADB_ROOT_PASSWORD") else None
        )
        random_root = _truthy(
            env.get("MYSQL_RANDOM_ROOT_PASSWORD")
            or env.get("MARIADB_RANDOM_ROOT_PASSWORD")
            or ""
        )
        app_user = env.get("MYSQL_USER") or env.get("MARIADB_USER")
        app_pw = env.get("MYSQL_PASSWORD") or env.get("MARIADB_PASSWORD")
        app_key = "MYSQL_PASSWORD" if env.get("MYSQL_PASSWORD") else (
            "MARIADB_PASSWORD" if env.get("MARIADB_PASSWORD") else None
        )
        app_db = env.get("MYSQL_DATABASE") or env.get("MARIADB_DATABASE")

        # root only if a real root password is present and NOT random
        if root_pw and not random_root:
            return {
                "engine": "mysql",
                "user": "root",
                "password": root_pw,
                "password_env_key": root_key,
                "all_databases": True,
                "databases": [],
                "source": "root",
            }
        # fall back to the app user (e.g. with MYSQL_RANDOM_ROOT_PASSWORD=yes)
        if app_user and app_pw:
            return {
                "engine": "mysql",
                "user": app_user,
                "password": app_pw,
                "password_env_key": app_key,
                "all_databases": False,
                "databases": [app_db] if app_db else [],
                "source": "app",
            }
        return {
            "engine": "mysql",
            "user": app_user or "root",
            "password": None,
            "password_env_key": None,
            "all_databases": not bool(app_db),
            "databases": [app_db] if app_db else [],
            "source": "unknown",
        }

    if engine == "postgres":
        pw = env.get("POSTGRES_PASSWORD")
        if flavor == "supabase":
            # Supabase: dump as superuser, preserve roles/privileges and include
            # the internal second DB (_supabase: analytics/pooler).
            user = env.get("POSTGRES_USER") or "supabase_admin"
            main_db = env.get("POSTGRES_DB") or "postgres"
            databases = [main_db]
            if _SUPABASE_INTERNAL_DB not in databases:
                databases.append(_SUPABASE_INTERNAL_DB)
            return {
                "engine": "postgres",
                "user": user,
                "password": pw,
                "password_env_key": "POSTGRES_PASSWORD" if pw else None,
                "all_databases": False,
                "databases": databases,
                "dump_globals": True,
                "source": "supabase",
            }
        user = env.get("POSTGRES_USER") or "postgres"
        db = env.get("POSTGRES_DB") or user
        return {
            "engine": "postgres",
            "user": user,
            "password": pw,
            "password_env_key": "POSTGRES_PASSWORD" if pw else None,
            "all_databases": False,
            "databases": [db],
            "dump_globals": False,
            "source": "postgres" if pw else "unknown",
        }

    return None
