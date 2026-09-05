"""Adaptateur RemoteOK (US-2.3.1).

API publique, sans clé : `https://remoteok.com/api`

Deux particularités, toutes deux documentées dans `docs/sources-audit.md` :

- la **première entrée du tableau n'est pas une offre** mais les mentions
  légales de l'API (`{"legal": ..., "last_updated": ...}`) — elle est
  sautée ;
- l'endpoint répond en **~40 s**, bien au-delà du défaut de 15 s : d'où le
  `default_timeout` dédié, sans quoi la source serait exclue de fait.

Conditions d'utilisation de RemoteOK : citer RemoteOK comme source et
pointer vers l'URL de l'offre. Le pipeline republie `RawJob.url`, qui est
l'URL RemoteOK, ce qui satisfait cette demande.
"""

from __future__ import annotations

import logging
import re

from src.adapters.base import (
    REMOTE,
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    to_iso_utc,
)
from src.core.config import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://remoteok.com/api"


class RemoteOkAdapter(AggregatorAdapter):
    ats = "remoteok"
    site_url = "https://remoteok.com/"
    #: L'API répond en ~40 s (audit du 2026-09-05).
    default_timeout = 60.0

    def _entries(self, source: Source) -> list[object]:
        payload = self._request_json(source, BASE_URL)
        if payload is None:
            return []

        entries = self._expect_list(source, payload, "réponse")
        # L'entête légale se reconnaît à sa clé `legal` et à son absence de
        # `position` : on la saute sans la journaliser, ce n'est pas une offre
        # cassée mais une entrée attendue du format.
        return [
            entry
            for entry in entries
            if not (isinstance(entry, dict) and "legal" in entry and "position" not in entry)
        ]

    def _parse(self, source: Source, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            logger.warning(
                "agrégateur « %s » (remoteok) : offre ignorée, mapping attendu", source.nom
            )
            return None

        # `id` est une chaine dans les reponses observees, un entier ailleurs.
        job_id = str(entry.get("id") or "").strip()
        titre = clean_str(entry.get("position"))
        if not job_id or not titre:
            logger.warning(
                "agrégateur « %s » (remoteok) : offre ignorée, « id » ou « position » manquant",
                source.nom,
            )
            return None

        localisation = clean_str(entry.get("location")).strip(" ,")
        url = clean_str(entry.get("url")) or clean_str(entry.get("apply_url"))

        return RawJob(
            id=job_id,
            ats=self.ats,
            # L'entreprise vient de l'offre : `Source.nom` ne nomme que la
            # plateforme (US-2.3.0).
            entreprise=clean_str(entry.get("company")),
            titre=titre,
            localisation=localisation,
            # RemoteOK ne publie que du télétravail : le libellé de
            # localisation n'est qu'une préférence de fuseau ou de pays, et
            # ne doit jamais faire retomber l'offre sur « unknown ».
            remote_type=infer_remote_type(localisation, fallback=REMOTE),
            url=url,
            # `date` est déjà en ISO ; `epoch` sert de repli.
            date=to_iso_utc(entry.get("date")) or to_iso_utc(entry.get("epoch")),
            description=repair_mojibake(html_to_text(clean_str(entry.get("description")))),
        )


def repair_mojibake(text: str) -> str:
    """Repare le double encodage UTF-8 des descriptions RemoteOK.

    RemoteOK sert ses textes non-anglais en mojibake : les octets UTF-8 d'un
    caractere accentue y sont re-encodes comme s'ils etaient du latin-1
    (`referAncia` au lieu de `referencia` accentue). Le defaut est **dans les
    donnees de la source**, pas dans notre decodage.

    On ne reecrit que les sequences qui portent la signature du double
    encodage -- un caractere de tete dans la plage des octets UTF-8 d'entete,
    suivi de ses octets de continuation -- et uniquement si elles se
    redecodent proprement. Le reste du texte, y compris les caracteres hors
    latin-1 (tirets longs, emojis) qui feraient echouer une conversion
    globale, est laisse intact.
    """
    if not text:
        return text
    return _MOJIBAKE_RE.sub(_decode_sequence, text)


#: Signature du double encodage : l'octet d'entete d'un caractere UTF-8
#: multi-octets, lu comme du latin-1, suivi du bon nombre d'octets de
#: continuation -- trois octets d'abord (l'apostrophe typographique, tres
#: frequente, en fait partie), puis deux.
_MOJIBAKE_RE = re.compile(
    "[\u00e0-\u00ef][\u0080-\u00bf]{2}|[\u00c2-\u00df][\u0080-\u00bf]"
)


def _decode_sequence(match: "re.Match[str]") -> str:
    try:
        return match.group(0).encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return match.group(0)
