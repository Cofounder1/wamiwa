"""Epic 15, the Wamiwa Agent: paid smart-mention cards, sponsored replies and suggested sellers.

What a paying account gets:
  * its name becomes tappable wherever an adult mentions it, opening a card with its link;
  * when someone asks about it, the Wamiwa Agent replies once in the thread with the link;
  * (businesses and sellers) a place in the "Suggested sellers" list for its category, shown when
    someone says they want that kind of product without naming a brand.

Everything here is advertising, so it is labeled Sponsored, never touches 13-17 accounts, and is
decided once, when the post or comment is written.
"""
import hashlib
import re
import secrets
from dataclasses import dataclass, field as dc_field
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, func, select, update

from app.core.enums import (
    AccountStatus, AccountType, AgentCommentPolicy, Label, ModerationStatus, NotificationType,
    PaymentPurpose, Permission, Queue,
)
from app.core.exceptions import Conflict, Forbidden, Gone, NotFound, PaymentRequired, Unprocessable
from app.core.utils import extract_mentions
from app.core import regions as core_regions
from app.models import (
    Account, AgentProfile, Comment, CompanyProfile, Contract, ExternalSocialLink, Post, PromoCategoryKeyword,
    PromoClaim, PromoDailyStat, PromoLink, PromoListing, PromoMatch, PromoName, QueueItem,
)
from app.services.base import BaseService, Principal

KINDS = ("business", "seller", "artist", "label")
LISTED_KINDS = ("business", "seller")  # who appears in "Suggested sellers"
TIERS = ("base", "plus")
# Where an artist's default link is looked for, in order: the first platform they are found on is used.
MUSIC_PLATFORMS = (("spotify", "Spotify"), ("youtube_music", "YouTube Music"), ("audiomack", "Audiomack"))
DEFAULT_PREFIX = "default-"
REPORTS_TO_PAUSE_LINK = 3
# Words artists commonly add around their name in a handle: "oshamo_music", "theoshamo", "oshamoofficial".
HANDLE_AFFIXES = ("official", "music", "musik", "hq", "real", "live", "tv", "ng", "worldwide", "the", "iam", "its")


@dataclass(frozen=True)
class SharedLink:
    """Something the agent can share: a link the owner added, or the default when they added none."""

    id: str
    url: str
    label: str
    name_id: str | None = None
    keywords: list = dc_field(default_factory=list)
    is_default: bool = False


STAT_FIELDS = ("card_impressions", "card_opens", "link_clicks", "agent_replies", "list_impressions", "list_clicks")

# Names that are also everyday words only match with an @, quotes or a cue word ("the brand called Shell").
COMMON_WORDS = frozenset("""
able about above after again air all also always america and angel any apple are area art away baby back bad
ball bank base beauty because bed been before best better big bird black blue boat body book born both box boy
bread bright bring brother build business call came can car care case cash cat chance change child church city
class clean clear close cloud club cold come cool could country cover crown cup dark day deal dear deep diamond
did does dog done door down dream dress drink drive drop each early earth east easy eat end energy enough even
ever every eye face fact fall family far farm fast father feel few field fine fire first fish five flow fly food
foot for form four free fresh friend from front full future game garden gave get gift girl give glass goes gold
good got great green ground group grow had hair half hand happy hard has have head hear heart heat help her here
high him his hold home hope horse hot hour house how idea into island its just keep kind king know lady land
large last late learn left less let life light like line list little live long look lost love low made magic
main make man many mark master may mean men might mile mind mine miss money moon more morning most mother move
much music must name near need never new next nice night north not note nothing now number ocean off often oil
old once one only open orange order other our out over own page paper part pass past pay peace people phone
place plain plan plant play point power press pure put queen quick rain reach read ready real red rest rich
right river road rock room rose round royal run safe said same sand save saw say sea second see seem self send
set she shell ship shop short should show side silver simple sing sister sit six sky small smart snow some song
soon sound south space spring star start state stay step still stone stop story street strong such summer sun
sure sweet table take talk tell ten than thank that the their them then there these they thing think this those
three through time today together too top touch town tree true try turn two under unit until upon use very view
voice wait walk want war warm was watch water wave way week well went were west what when where which white who
why will wind winter wish with woman wonder word work world would write year yes yet you young your zero
""".split())
CUE_WORDS = r"(?:called|named|by|from|at|artist|artiste|singer|rapper|band|brand|label|company|shop|store|seller)"

# A question about someone or something: the Wamiwa Agent may answer.
ASK = re.compile(
    r"\bwho(?:'s| is| are| be)\b|\bwhere (?:can|do|could|fit) (?:i|we|one)\b|\bwhere to (?:buy|listen|find|get|stream)\b"
    r"|\bhow (?:can|do) (?:i|we) (?:get|buy|find|listen|reach|contact)\b|\b(?:link|website|page|handle)\b[^.!]*\?"
    r"|\bwetin be\b|\banyone know\b", re.I)
# A wish to buy something, with no brand named.
WANT = re.compile(
    r"\bi (?:really |just |so )?(?:love|like|want|need|wan)\b|\blooking for\b|\bwhere (?:can|fit) i (?:get|buy|find|see)\b"
    r"|\banyone sell(?:ing|s)?\b|\bwho sells?\b|\brecommend\b|\bin need of\b|\babeg where\b", re.I)
NOT_WANT = re.compile(r"\b(?:don'?t|do not|never|no longer|used to|hate|stopped)\b", re.I)
COMPLAINT = re.compile(
    r"\b(?:scam(?:med|mer|s)?|fraud|fake|stole|steal|thie(?:f|ves)|refund|rip(?:ped)? ?off|cheat(?:ed|s)?|worst"
    r"|terrible|awful|useless|rubbish|avoid|never again|disappoint\w*|sue|lawsuit|boycott|419|wack|nonsense)\b", re.I)
SENSITIVE = re.compile(
    r"\b(?:die[ds]?|dead|death|funeral|burial|cancer|hospital|sick(?:ness)?|illness|depress\w*|suicid\w*|debt|broke"
    r"|bankrupt\w*|miscarriage|rape[ds]?|abuse[ds]?|assault\w*|grie(?:f|ving)|mourn\w*|accident|divorce)\b", re.I)

# Art: recurring cultural moments by region, as (month, day, name). "*" applies everywhere.
# A starting list for the editorial team to grow; moving feasts (Eid, Lunar New Year) need yearly dates.
CULTURAL_MOMENTS: dict[str, list[tuple[int, int, str]]] = {
    "*": [(1, 1, "New Year"), (2, 14, "Valentine's Day"), (3, 8, "International Women's Day"),
          (10, 31, "Halloween"), (12, 25, "Christmas")],
    "nigeria": [(5, 27, "Children's Day"), (6, 12, "Democracy Day"), (10, 1, "Independence Day"),
                (12, 1, "the start of Detty December")],
    "ghana": [(3, 6, "Independence Day"), (12, 1, "December in GH")],
    "united_states": [(2, 1, "Black History Month"), (7, 4, "Independence Day"), (11, 26, "Thanksgiving week"),
           (11, 27, "Black Friday")],
    "united_kingdom": [(8, 24, "Notting Hill Carnival"), (11, 5, "Bonfire Night"), (11, 27, "Black Friday")],
    "china": [(6, 18, "the 618 shopping festival"), (10, 1, "National Day Golden Week"), (11, 11, "Singles' Day")],
    "india": [(8, 15, "Independence Day"), (11, 1, "the Diwali season")],
    "brazil": [(2, 14, "Carnival season"), (6, 12, "Dia dos Namorados")],
}
# Art: forms that carry a topic. {topic} is what is rising, {area} is what the owner does.
CREATIVE_FORMATS = (
    "Tell a 30-second story that links {topic} to {area}: open on the moment, end on the lesson.",
    "Take a side on {topic} from where you stand in {area}, and ask people to disagree with you.",
    "Show the behind-the-scenes of {area} through the lens of {topic}. Unpolished beats perfect.",
    "Start a challenge around {topic} that anyone can copy in under a minute, and do it first.",
    "Explain {topic} to someone outside {area} as if to a friend, in three plain steps.",
    "Pair {topic} with something from your own roots that people would not expect, and name the contrast.",
)


def normalize_name(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s&'.-]", "", name.lower()).split())


def squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def handle_matches(name: str, handle: str) -> bool:
    """Whether a handle is plainly this name, allowing the usual extras ("oshamo_music" is "Oshamo")."""
    target, candidate = squash(name), squash(handle)
    if not target or not candidate:
        return False
    changed = True
    while candidate != target and changed:
        changed = False
        for affix in HANDLE_AFFIXES:
            if candidate.startswith(affix) and len(candidate) > len(affix):
                candidate, changed = candidate[len(affix):], True
            elif candidate.endswith(affix) and len(candidate) > len(affix):
                candidate, changed = candidate[:-len(affix)], True
    return candidate == target


def is_distinctive(normalized: str) -> bool:
    """Safe to match in plain text: long enough and not just everyday words."""
    words = normalized.split()
    return len(normalized) >= 4 and not all(word in COMMON_WORDS for word in words)


class PromoService(BaseService):
    # --- the Wamiwa Agent account ------------------------------------------------------------
    async def ensure_agent(self) -> Account:
        """The first-party agent, listed in the marketplace. Created on first use."""
        handle = self.s.wamiwa_agent_handle
        agent = await self.repo(Account).first(Account.handle == handle, Account.type == AccountType.AGENT)
        if agent is not None:
            return agent
        owner = await self.repo(Account).first(Account.handle == f"{handle}_official")
        if owner is None:
            owner = await self.repo(Account).add(Account(
                type=AccountType.COMPANY, handle=f"{handle}_official", display_name="Wamiwa"))
            await self.repo(CompanyProfile).add(CompanyProfile(
                account_id=owner.id, business_name="Wamiwa", category="platform", verified=True, b2b_opt_out=True))
        agent = await self.repo(Account).add(Account(type=AccountType.AGENT, handle=handle, display_name="Wamiwa Agent"))
        await self.repo(AgentProfile).add(AgentProfile(
            account_id=agent.id, owner_account_id=owner.id, name="Wamiwa Agent",
            description=("Makes your name tappable wherever people mention you, answers people who ask about you "
                         "with your link, and lists you under Suggested sellers. Monthly plan."),
            skills=["smart mentions", "suggested sellers", "link sharing", "trend ideas"],
            rates={"smart mentions (monthly plan)": self.s.promo_plan_monthly_cents,
                   "record label roster (monthly plan)": self.s.promo_label_monthly_cents,
                   "plus package (add-on)": self.s.promo_plus_addon_cents},
            min_rate_cents=self.s.promo_plan_monthly_cents))
        await self.cache.invalidate_prefix("marketplace:")
        return agent

    # --- buying and managing a plan -------------------------------------------------------------
    def price_cents(self, kind: str, tier: str = "base") -> int:
        base = self.s.promo_label_monthly_cents if kind == "label" else self.s.promo_plan_monthly_cents
        return base + (self.s.promo_plus_addon_cents if tier == "plus" else 0)

    def link_limit(self, kind: str, tier: str) -> int:
        """Links the agent will share: 5 for businesses and sellers, 3 for artists and for each artist
        on a label's roster. The Plus package doubles it."""
        base = self.s.promo_links_business if kind in LISTED_KINDS else self.s.promo_links_artist
        return base * 2 if tier == "plus" else base

    async def _eligibility(self, account: Account, kind: str) -> None:
        if kind not in KINDS:
            raise Unprocessable(f"kind must be one of {', '.join(KINDS)}", code="invalid_kind")
        if kind in ("business", "label"):
            await self.svc.access.require_verified_company(account, "use the Wamiwa Agent")
        else:
            if not account.is_user:
                raise Unprocessable("Companies buy the business or label plan", code="invalid_kind")
            await self.svc.access.require_adult(account, "use the Wamiwa Agent")
            await self.svc.access.require_id_verified(account, "use the Wamiwa Agent")

    @staticmethod
    def _check_url(url: str) -> str:
        parsed = urlparse(url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or "." not in host or host == "localhost" or re.fullmatch(r"[\d.]+", host):
            raise Unprocessable("Links must be public https addresses", code="invalid_link")
        return url

    def _regions(self, regions: list[str] | None) -> list[str]:
        return core_regions.require_many(regions, self.s.promo_max_target_regions)

    async def plans(self, account: Account) -> dict[str, Any]:
        agent = await self.ensure_agent()
        rate = await self.svc.fx.rate()
        prices = {}
        for kind in KINDS:
            prices[kind] = {}
            for tier in TIERS:
                local, _ = await self.svc.fx.to_local(self.price_cents(kind, tier))
                prices[kind][tier] = {"usd_cents": self.price_cents(kind, tier), "local": local,
                                      "links": self.link_limit(kind, tier)}
        return {
            "agent": {"id": agent.id, "handle": agent.handle, "name": "Wamiwa Agent"},
            "currency": self.s.local_currency, "fx_rate": str(rate), "period_days": self.s.promo_period_days,
            "prices": prices, "label_roster_size": self.s.promo_max_names,
            "packages": {
                "base": "Name card, replies when people ask about you, and the links you choose for the agent to share",
                "plus": "Twice the links, plus content ideas built on what is likely to trend in the regions you target",
            },
            "kinds": {
                "business": "Verified companies. Also listed under Suggested sellers",
                "seller": "ID-verified individuals who sell. Shop name and links are reviewed first. Also listed under Suggested sellers",
                "artist": "ID-verified artists. Add the links you want shared: streaming profiles, a new single, tickets",
                "label": "Verified labels. Add each artist on your roster and the links to share for each",
            },
            "rules": ["Shown to adults only, never on posts or comments by 13-17 accounts",
                      "Labeled Sponsored", "Not shown on complaints or sensitive topics",
                      "People can switch sponsored cards off on their own posts and comments",
                      "Your links are kept if your plan ends, and work again when you buy the agent again"],
        }

    def _present_link(self, link: PromoLink, active_ids: set[str]) -> dict[str, Any]:
        return {"id": link.id, "url": link.url, "label": link.label, "keywords": link.keywords or [],
                "name_id": link.name_id, "shared_by_agent": link.id in active_ids,
                "under_review": link.flagged, "review_reason": link.flag_reason if link.flagged else None}

    async def _links(self, listing: PromoListing) -> tuple[list[PromoLink], set[str]]:
        """All saved links, and the ids of those the agent will actually share right now: none unless
        the plan is active, and within the plan's limit (oldest first) for each artist or account."""
        links = list(await self.repo(PromoLink).list(PromoLink.listing_id == listing.id, order_by=PromoLink.id))
        active: set[str] = set()
        if listing.status == "active":
            limit, used = self.link_limit(listing.kind, listing.tier), {}
            for link in links:
                scope = link.name_id or ""
                if not link.flagged and used.get(scope, 0) < limit:
                    used[scope] = used.get(scope, 0) + 1
                    active.add(link.id)
        return links, active

    async def mine(self, account: Account) -> dict[str, Any] | None:
        listing = await self.repo(PromoListing).first(PromoListing.account_id == account.id)
        if listing is None:
            return None
        names = await self.repo(PromoName).list(PromoName.listing_id == listing.id)
        links, active = await self._links(listing)
        return {
            "id": listing.id, "kind": listing.kind, "tier": listing.tier, "status": listing.status,
            "category": listing.category, "target_regions": listing.target_regions or [],
            "cancel_at_period_end": listing.cancel_at_period_end,
            "renews_at": listing.renews_at.isoformat() if listing.renews_at else None,
            "price_cents": self.price_cents(listing.kind, listing.tier),
            "link_limit": self.link_limit(listing.kind, listing.tier),
            "link_limit_applies": "per artist" if listing.kind == "label" else "to the plan",
            "names": [{"id": n.id, "name": n.name, "status": n.status, "matches_in_plain_text": n.plain_text_match,
                       "default_link": None if not n.default_link_url else {
                           "url": n.default_link_url, "label": n.default_link_label}} for n in names],
            "links": [self._present_link(link, active) for link in links],
            "default_link_rule": {
                "artist": "With no links added, the agent shares the profile we found on Spotify, YouTube Music or Audiomack",
                "label": "For an artist with no links, the agent shares the profile we found on Spotify, YouTube Music or Audiomack",
                "business": "With no links added, the agent shares the website on your company profile",
                "seller": "Add at least one link for the agent to share",
            }[listing.kind],
        }

    async def _listing(self, account: Account) -> PromoListing:
        listing = await self.repo(PromoListing).first(PromoListing.account_id == account.id)
        if listing is None:
            raise NotFound("You do not have a Wamiwa Agent plan", code="no_plan", hint="POST /promotions")
        return listing

    async def subscribe(self, account: Account, *, kind: str, tier: str, category: str | None, brand_name: str | None,
                        links: list[dict[str, Any]], target_regions: list[str] | None, payment_method: str) -> dict[str, Any]:
        await self._eligibility(account, kind)
        if tier not in TIERS:
            raise Unprocessable("tier must be base or plus", code="invalid_tier")
        listing = await self.repo(PromoListing).first(PromoListing.account_id == account.id)
        if listing and listing.status in ("active", "paused", "pending_review") and not listing.cancel_at_period_end:
            raise Conflict("You already have a Wamiwa Agent plan", code="plan_active")
        company = await self.svc.access.company(account.id)
        category = (category or (company.category if company else None) or "").strip().lower().replace(" ", "_") or None
        if kind in LISTED_KINDS and not category:
            raise Unprocessable("Choose the category you sell in", code="category_required")
        if kind in ("seller", "artist") and not (brand_name or "").strip() and listing is None:
            raise Unprocessable("Tell us your shop name" if kind == "seller" else "Tell us your stage name",
                                code="brand_name_required")
        regions = self._regions(target_regions)
        for link in links:
            self._check_url(link["url"])
        if len(links) > self.link_limit(kind, tier) and kind != "label":
            raise Unprocessable(f"This package shares up to {self.link_limit(kind, tier)} links", code="too_many_links",
                                hint="Choose the Plus package for more")
        if kind == "label" and links:
            raise Unprocessable("Add each artist first, then the links for that artist", code="links_need_artist")

        # Check the name before taking any money.
        main_name = brand_name if kind in ("seller", "artist") else company.business_name
        if main_name and not 2 <= len(normalize_name(main_name)) <= 80:
            raise Unprocessable("Names must be 2 to 80 characters", code="invalid_name")
        if main_name and await self._claimed_by_other(normalize_name(main_name), listing.id if listing else ""):
            raise Conflict("That name already belongs to another verified account. Contact support if it is yours.",
                           code="name_taken", hint="POST /support/tickets")

        payment = await self.svc.payment.charge(account, PaymentPurpose.PROMOTION, self.price_cents(kind, tier), payment_method)
        needs_review = kind == "seller"  # an individual's shop name and links are checked by a person first
        if listing is None:
            listing = await self.repo(PromoListing).add(PromoListing(
                account_id=account.id, kind=kind, status="pending_review", payment_method=payment_method))
        listing.kind, listing.tier, listing.category, listing.target_regions = kind, tier, category, regions
        listing.payment_method, listing.last_payment_id, listing.cancel_at_period_end = payment_method, payment.id, False
        listing.status = "pending_review" if needs_review else "active"
        listing.renews_at = None if needs_review else self.now() + timedelta(days=self.s.promo_period_days)

        if main_name:
            # A seller's name is checked with the plan. A company's registered name is its own. An artist's
            # stage name goes live at once only if they have already proven it is theirs.
            proven = kind in ("business", "label") or (kind == "artist" and await self.proves_name(account, main_name))
            await self._add_name(listing, account, main_name, auto_approve=proven and not needs_review, primary=True,
                                 enqueue=not needs_review)
        for link in links:
            await self._save_link(listing, account, link["url"], link["label"], self._keywords(link), None)
        if needs_review:
            await self.svc.queues.enqueue(
                Queue.PROMOTIONS, "promo_listing", listing.id, subject_account_id=account.id,
                summary=f"Seller plan: {main_name or 'renewal'} ({category}) " + " ".join(l["url"] for l in links))
        await self._invalidate()
        return await self.mine(account)

    @staticmethod
    def _keywords(link: dict[str, Any]) -> list[str]:
        return sorted({k.strip().lower() for k in link.get("keywords") or [] if len(k.strip()) >= 3})[:10]

    async def update(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        listing = await self._listing(account)
        if changes.get("category") is not None:
            listing.category = changes["category"].strip().lower().replace(" ", "_") or listing.category
        if changes.get("target_regions") is not None:
            listing.target_regions = self._regions(changes["target_regions"])  # any regions, wherever the owner is
            await self.cache.delete(f"trendideas:{listing.id}")
        if changes.get("paused") is not None and listing.status in ("active", "paused"):
            listing.status = "paused" if changes["paused"] else "active"
        tier = changes.get("tier")
        if tier is not None and tier != listing.tier:
            if tier not in TIERS:
                raise Unprocessable("tier must be base or plus", code="invalid_tier")
            if tier == "plus" and listing.status in ("active", "paused"):
                # Moving up starts at once: the add-on is charged now and with each renewal after.
                if not changes.get("payment_method") and not listing.payment_method:
                    raise PaymentRequired("Send a payment method to upgrade", code="payment_required")
                await self.svc.payment.charge(account, PaymentPurpose.PROMOTION, self.s.promo_plus_addon_cents,
                                              changes.get("payment_method") or listing.payment_method)
            # Moving down keeps every saved link, but only the first few within the smaller limit are shared.
            listing.tier = tier
        await self._invalidate()
        return await self.mine(account)

    async def cancel(self, account: Account) -> dict[str, Any]:
        """Takes effect at the end of the paid month."""
        listing = await self._listing(account)
        listing.cancel_at_period_end = True
        return await self.mine(account)

    async def renew_due(self) -> dict[str, int]:
        """Job: monthly renewal. An unpaid or cancelled plan lapses: its cards disappear and its links
        stop working, but names and links stay saved for when the agent is bought again."""
        due = await self.repo(PromoListing).list(
            PromoListing.status.in_(["active", "paused"]), PromoListing.renews_at.is_not(None),
            PromoListing.renews_at <= self.now())
        renewed = lapsed = 0
        for listing in due:
            account = await self.repo(Account).require(listing.account_id)
            paid = False
            if not listing.cancel_at_period_end and account.status == AccountStatus.ACTIVE:
                try:
                    payment = await self.svc.payment.charge(
                        account, PaymentPurpose.PROMOTION, self.price_cents(listing.kind, listing.tier),
                        listing.payment_method)
                    listing.last_payment_id, paid = payment.id, True
                except PaymentRequired:
                    pass
            if paid:
                listing.renews_at += timedelta(days=self.s.promo_period_days)
                renewed += 1
            else:
                listing.status = "lapsed"
                lapsed += 1
                await self.svc.notifications.notify(
                    account.id, NotificationType.PAYMENT, "Your Wamiwa Agent plan has ended",
                    "Your name cards, replies and links are switched off. They are saved and will work again "
                    "when you buy the agent again.")
        if due:
            await self._invalidate()
        return {"renewed": renewed, "lapsed": lapsed}

    # --- names: who may claim one ------------------------------------------------------------------
    async def _add_name(self, listing: PromoListing, account: Account, name: str, *, auto_approve: bool,
                        primary: bool = False, enqueue: bool = True) -> PromoName:
        name = " ".join((name or "").split())
        normalized = normalize_name(name)
        if len(normalized) < 2 or len(name) > 80:
            raise Unprocessable("Names must be 2 to 80 characters", code="invalid_name")
        existing = await self.repo(PromoName).first(PromoName.listing_id == listing.id, PromoName.normalized == normalized)
        if existing is not None:
            if existing.status == "rejected":
                existing.status = "pending"
            return existing
        if await self.repo(PromoName).count(PromoName.listing_id == listing.id) >= self.s.promo_max_names + 1:
            raise Conflict(f"One plan holds up to {self.s.promo_max_names} artists", code="too_many_names")
        if await self._claimed_by_other(normalized, listing.id):
            raise Conflict("That name already belongs to another verified account. Contact support if it is yours.",
                           code="name_taken", hint="POST /support/tickets")
        # A name that is someone else's handle or registered business, or a protected public figure,
        # is never approved automatically: a person checks it.
        contested = await self._belongs_to_someone_else(normalized, account)
        reserved_for = await self.kv.get(f"promo:reserved:{normalized}")
        if reserved_for == account.id:
            auto_approve, contested = True, False
        elif reserved_for:
            raise Conflict("That name already belongs to another verified account. Contact support if it is yours.",
                           code="name_taken", hint="POST /support/tickets")
        row = await self.repo(PromoName).add(PromoName(
            listing_id=listing.id, name=name, normalized=normalized, primary=primary,
            plain_text_match=is_distinctive(normalized),
            status="approved" if auto_approve and not contested else "pending"))
        if row.status == "pending" and enqueue:
            await self.svc.queues.enqueue(Queue.PROMOTIONS, "promo_name", row.id, subject_account_id=account.id,
                                          summary=f"Name claim: {name}" + (" (contested)" if contested else ""))
        if listing.kind in ("artist", "label"):
            await self._find_default(row)
        return row

    async def _find_default(self, name: PromoName) -> None:
        """An artist's default link: their profile on Spotify, or failing that YouTube Music, or failing
        that Audiomack. Looked up one platform at a time and remembered."""
        name.default_link_url, name.default_link_label = None, None
        for platform, label in MUSIC_PLATFORMS:
            url = await self.providers.music.find_artist(platform, name.name)
            if url and not (await self.providers.link_safety.check(url)).suspicious:
                name.default_link_url, name.default_link_label = url, label
                break
        name.default_checked_at = self.now()

    async def refresh_defaults(self) -> int:
        """Job: look again each week, so an artist who has since joined a platform is picked up."""
        stale = await self.repo(PromoName).list(
            (PromoName.default_checked_at.is_(None)) | (PromoName.default_checked_at < self.now() - timedelta(days=7)))
        kinds = {l.id: l.kind for l in (await self.repo(PromoListing).by_ids([n.listing_id for n in stale])).values()}
        refreshed = 0
        for name in stale:
            if kinds.get(name.listing_id) in ("artist", "label"):
                await self._find_default(name)
                refreshed += 1
        if refreshed:
            await self._invalidate()
        return refreshed

    async def _claimed_by_other(self, normalized: str, listing_id: str) -> bool:
        return await self.repo(PromoName).exists(
            PromoName.normalized == normalized, PromoName.listing_id != listing_id,
            PromoName.status.in_(["approved", "disputed"]))

    async def _belongs_to_someone_else(self, normalized: str, account: Account) -> bool:
        handle = re.sub(r"[^a-z0-9_]", "", normalized.replace(" ", "_"))
        if handle and await self.repo(Account).exists(Account.handle == handle, Account.id != account.id):
            return True
        if await self.repo(CompanyProfile).exists(
                func.lower(CompanyProfile.business_name) == normalized, CompanyProfile.account_id != account.id,
                CompanyProfile.verified.is_(True)):
            return True
        profile = await self.svc.access.profile(account.id)
        return await self.providers.identity.is_protected_identity(normalized) and not (profile and profile.is_major_person)

    async def add_name(self, account: Account, name: str) -> dict[str, Any]:
        """Labels add each artist on their roster; artists add another stage name. These go live at once
        unless the name looks like someone else's. Other names a business or seller trades under are
        checked by a person first."""
        listing = await self._listing(account)
        proven = listing.kind == "label" or (listing.kind == "artist" and await self.proves_name(account, name))
        await self._add_name(listing, account, name, auto_approve=proven)
        await self._invalidate()
        return await self.mine(account)

    async def proves_name(self, account: Account, name: str) -> bool:
        """Has this account already shown the name is theirs? True when it matches their Wamiwa handle,
        their verified name, a social account they proved by logging in (ON-07), or they are a Verified
        major person. Nothing new is asked of them."""
        if handle_matches(name, account.handle):
            return True
        profile = await self.svc.access.profile(account.id)
        if profile and (profile.is_major_person or squash(profile.verified_name or "") == squash(name)):
            return True
        socials = await self.repo(ExternalSocialLink).list(
            ExternalSocialLink.account_id == account.id, ExternalSocialLink.connected.is_(True),
            ExternalSocialLink.flagged.is_(False))
        return any(handle_matches(name, social.handle) for social in socials)

    # --- "this is my name" claims -------------------------------------------------------------------
    async def claim_name(self, claimant: Account, name: str, evidence: str) -> dict[str, Any]:
        """A real artist or business finds their name on someone else's plan. If they can already show
        it is theirs, its cards stop at once while an admin decides; otherwise it stays live until then,
        so a rival cannot switch someone off just by complaining."""
        if claimant.is_company:
            await self.svc.access.require_verified_company(claimant, "claim a name")
        else:
            await self.svc.access.require_adult(claimant, "claim a name")
            await self.svc.access.require_id_verified(claimant, "claim a name")
        normalized = normalize_name(name)
        mine = await self.repo(PromoListing).first(PromoListing.account_id == claimant.id)
        held = await self.repo(PromoName).first(
            PromoName.normalized == normalized, PromoName.status.in_(["approved", "disputed"]),
            PromoName.listing_id != (mine.id if mine else ""))
        if held is None:
            raise NotFound("Nobody else holds that name. You can add it to your own plan.", code="name_not_held")
        if await self.repo(PromoClaim).exists(PromoClaim.name_id == held.id, PromoClaim.status == "open",
                                              PromoClaim.claimant_account_id == claimant.id):
            raise Conflict("You have already claimed this name", code="already_claimed")
        await self.limiter.enforce(self.policies.reports, f"claim:{claimant.id}")
        company = await self.svc.access.company(claimant.id)
        shown = await self.proves_name(claimant, name) or bool(
            company and company.verified and normalize_name(company.business_name) == normalized)
        claim = await self.repo(PromoClaim).add(PromoClaim(
            name_id=held.id, normalized=normalized, claimant_account_id=claimant.id, evidence=evidence.strip(),
            froze_name=shown))
        holder = await self.repo(PromoListing).require(held.listing_id)
        if shown:
            held.status = "disputed"
            await self.svc.notifications.notify(
                holder.account_id, NotificationType.LEGAL, f"The name {held.name} is in dispute",
                "Someone has shown it may be theirs. Its cards are paused while our team decides.")
        await self.svc.queues.enqueue(
            Queue.PROMOTIONS, "promo_claim", claim.id, subject_account_id=holder.account_id, severity="high",
            summary=f"@{claimant.handle} claims the name {held.name}" + (" (paused: claimant matches it)" if shown else ""))
        await self._invalidate()
        return {"claim_id": claim.id, "name": held.name, "paused_while_reviewed": shown}

    async def _decide_claim(self, claim: PromoClaim, upheld: bool, reason: str) -> None:
        held = await self.repo(PromoName).get(claim.name_id)
        claim.status = "upheld" if upheld else "dismissed"
        claimant = await self.repo(Account).require(claim.claimant_account_id)
        if held is not None:
            holder = await self.repo(PromoListing).require(held.listing_id)
            if upheld:
                for link in await self.repo(PromoLink).list(PromoLink.name_id == held.id):
                    await self.repo(PromoLink).delete(link)
                display = held.name
                if held.primary:
                    held.status = "rejected"  # the plan stays, but this name no longer works for it
                else:
                    await self.repo(PromoName).delete(held)
                # The name passes to the claimant: onto their plan now, or kept for them if they have none yet.
                theirs = await self.repo(PromoListing).first(PromoListing.account_id == claimant.id)
                if theirs is not None:
                    row = await self._add_name(theirs, claimant, display, auto_approve=True, enqueue=False)
                    row.status = "approved"
                else:
                    await self.kv.set(f"promo:reserved:{claim.normalized}", claimant.id, 180 * 86400)
            elif held.status == "disputed":
                held.status = "approved"
            await self.svc.notifications.notify(
                holder.account_id, NotificationType.LEGAL,
                f"The name {held.name} was removed from your plan" if upheld else f"The claim on {held.name} was dismissed",
                reason)
        await self.svc.notifications.notify(
            claimant.id, NotificationType.LEGAL,
            "Your name claim was upheld" if upheld else "Your name claim was not upheld", reason)

    async def _own_name(self, account: Account, name_id: str) -> tuple[PromoListing, PromoName]:
        listing = await self._listing(account)
        row = await self.repo(PromoName).get(name_id)
        if row is None or row.listing_id != listing.id:
            raise NotFound("Name not found")
        return listing, row

    async def remove_name(self, account: Account, name_id: str) -> dict[str, Any]:
        _, row = await self._own_name(account, name_id)
        if row.primary:
            raise Conflict("Your main name cannot be removed", code="primary_name")
        for link in await self.repo(PromoLink).list(PromoLink.name_id == row.id):
            await self.repo(PromoLink).delete(link)
        await self.repo(PromoName).delete(row)
        await self._invalidate()
        return await self.mine(account)

    # --- links the agent shares -------------------------------------------------------------------
    async def add_link(self, account: Account, url: str, label: str, keywords: list[str] | None,
                       name_id: str | None) -> dict[str, Any]:
        """Any link the owner wants shared: a site, a store, a streaming profile, a new single, tickets.
        Links can be added whatever the plan's state; they are only shared while the plan is active."""
        listing = await self._listing(account)
        self._check_url(url)
        if listing.kind == "label":
            if not name_id:
                raise Unprocessable("Say which artist this link is for", code="artist_required")
            await self._own_name(account, name_id)
        else:
            name_id = None  # everyone else's links belong to the plan as a whole
        scope = [PromoLink.listing_id == listing.id,
                 PromoLink.name_id == name_id if name_id else PromoLink.name_id.is_(None)]
        if await self.repo(PromoLink).exists(*scope, PromoLink.url == url):
            raise Conflict("You have already added this link", code="link_exists")
        limit = self.link_limit(listing.kind, listing.tier)
        if await self.repo(PromoLink).count(*scope) >= limit:
            where = "for this artist" if listing.kind == "label" else "on your package"
            raise Conflict(f"You can share up to {limit} links {where}", code="link_limit",
                           hint="Remove one, or move to the Plus package for twice as many" if listing.tier == "base"
                           else "Remove one to add another", limit=limit)
        link = await self._save_link(listing, account, url, label, self._keywords({"keywords": keywords}), name_id)
        if listing.kind == "seller" and listing.status in ("active", "paused") and not link.flagged:
            # An individual seller's new link is looked at before it goes in front of people.
            listing.status = "pending_review"
            await self.svc.queues.enqueue(Queue.PROMOTIONS, "promo_listing", listing.id,
                                          subject_account_id=account.id, summary=f"New link: {url}")
        await self._invalidate()
        result = await self.mine(account)
        if link.flagged:
            result["warning"] = (f"This link looks unsafe ({link.flag_reason}). It has been saved but will not be "
                                 "shared until our team has checked it.")
        return result

    # --- scam links ----------------------------------------------------------------------------------
    async def _save_link(self, listing: PromoListing, account: Account, url: str, label: str, keywords: list[str],
                         name_id: str | None) -> PromoLink:
        """Every link is checked when it is added. A suspect link is kept but never shared; the owner is
        warned and every Admin and Super Admin is told."""
        verdict = await self.providers.link_safety.check(url)
        link = await self.repo(PromoLink).add(PromoLink(
            listing_id=listing.id, name_id=name_id, url=url, label=label.strip(), keywords=keywords,
            flagged=verdict.suspicious, flag_reason="; ".join(verdict.reasons)[:300] or None))
        if verdict.suspicious:
            await self._raise_alarm(link, account.id, f"Added by @{account.handle}: {link.flag_reason}")
        return link

    async def _raise_alarm(self, link: PromoLink, owner_id: str, summary: str) -> None:
        await self.svc.queues.enqueue(Queue.PROMOTIONS, "promo_link", link.id, subject_account_id=owner_id,
                                      summary=f"Suspected scam link {link.url} - {summary}", severity="high")
        await self.svc.admin.alert_admins("Suspected scam link on a Wamiwa Agent plan",
                                          f"{link.url} - {summary}", {"link_id": link.id})
        await self.svc.notifications.notify(
            owner_id, NotificationType.SECURITY, "One of your links is being checked",
            f"{link.url} looks unsafe and is not being shared until our team has reviewed it.")

    async def vet_website(self, account: Account, url: str) -> str:
        """A company's website is its default link, so it gets the same check before it is saved."""
        self._check_url(url)
        verdict = await self.providers.link_safety.check(url)
        if verdict.suspicious:
            reasons = "; ".join(verdict.reasons)
            await self.svc.admin.alert_admins("Suspected scam website on a company profile",
                                              f"@{account.handle} tried to set {url} - {reasons}")
            raise Unprocessable(f"This address looks unsafe ({reasons}). Our team has been told.", code="unsafe_link")
        await self._invalidate()
        return url

    async def report_link(self, viewer: Account, link_id: str, details: str) -> dict[str, Any]:
        """Anyone can report a shared link. Admins are told at once; after a few different people report
        the same link it is switched off until reviewed."""
        if not await self._audience(viewer):
            raise NotFound("Link not found")
        await self.limiter.enforce(self.policies.reports, viewer.id)
        link = await self.repo(PromoLink).get(link_id)
        if link is None:
            raise NotFound("Link not found")
        listing = await self.repo(PromoListing).require(link.listing_id)
        if listing.account_id == viewer.id:
            raise Unprocessable("You cannot report your own link", code="self_report")
        if not await self.kv.set_if_absent(f"promo:linkreport:{link.id}:{viewer.id}", "1", 30 * 86400):
            raise Conflict("You have already reported this link", code="already_reported")
        link.reports += 1
        if link.reports >= REPORTS_TO_PAUSE_LINK and not link.flagged:
            link.flagged, link.flag_reason = True, f"reported by {link.reports} people"
        await self._raise_alarm(link, listing.account_id, f"Reported by @{viewer.handle}: {details[:200]}")
        await self._invalidate()
        return {"reported": True, "switched_off": link.flagged}

    async def recheck_links(self) -> int:
        """Job: a page that was fine yesterday can turn bad, so every shared link is checked again daily."""
        flagged = 0
        for link in await self.repo(PromoLink).list(PromoLink.flagged.is_(False)):
            verdict = await self.providers.link_safety.check(link.url)
            reasons = "; ".join(verdict.reasons)[:300]
            if verdict.suspicious and reasons != link.cleared_reasons:
                link.flagged, link.flag_reason = True, reasons
                listing = await self.repo(PromoListing).require(link.listing_id)
                await self._raise_alarm(link, listing.account_id, f"Failed the daily check: {link.flag_reason}")
                flagged += 1
        if flagged:
            await self._invalidate()
        return flagged

    async def remove_link(self, account: Account, link_id: str) -> dict[str, Any]:
        """Once a link is deleted the agent stops sharing it, and replies that carried it no longer open it."""
        listing = await self._listing(account)
        link = await self.repo(PromoLink).get(link_id)
        if link is None or link.listing_id != listing.id:
            raise NotFound("Link not found")
        await self.repo(PromoLink).delete(link)
        await self._invalidate()
        return await self.mine(account)

    async def _shareable(self, listing: PromoListing, name_id: str | None) -> list[SharedLink]:
        """What the agent may share for this name right now: the owner's own links, or, when they have
        added none, the default (an artist's streaming profile, a company's website)."""
        links, active = await self._links(listing)
        scope = name_id if listing.kind == "label" else None
        own = [SharedLink(l.id, l.url, l.label, l.name_id, l.keywords or []) for l in links
               if l.id in active and l.name_id == scope]
        if own or listing.status != "active":
            return own
        name = await self.repo(PromoName).get(name_id) if name_id else None
        if name is None or name.listing_id != listing.id:
            name = await self.repo(PromoName).first(PromoName.listing_id == listing.id, PromoName.primary.is_(True))
        if name is None:
            return []
        if listing.kind in ("artist", "label") and name.default_link_url:
            return [SharedLink(DEFAULT_PREFIX + name.id, name.default_link_url, name.default_link_label or "Listen",
                               name.id, is_default=True)]
        if listing.kind == "business":
            company = await self.svc.access.company(listing.account_id)
            if company and company.website:
                return [SharedLink(DEFAULT_PREFIX + name.id, company.website, "Website", name.id, is_default=True)]
        return []

    @staticmethod
    def _pick(links: list[SharedLink], text: str) -> SharedLink | None:
        """The link that fits what was said (someone asking for "tickets" gets the tickets link);
        otherwise one at random, so every link gets its turn."""
        if not links:
            return None
        lowered = (text or "").lower()
        fitting = [link for link in links if any(
            re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered)
            for word in [*(link.keywords or []), *link.label.lower().split()] if len(word) >= 3)]
        return secrets.choice(fitting or links)

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        if item.target_type == "promo_listing":
            listing = await self.repo(PromoListing).require(item.target_id)
            if listing.status != "pending_review":
                return
            if approved:
                listing.status = "active"
                listing.renews_at = listing.renews_at or self.now() + timedelta(days=self.s.promo_period_days)
                for row in await self.repo(PromoName).list(PromoName.listing_id == listing.id, PromoName.primary.is_(True)):
                    if not await self._claimed_by_other(row.normalized, listing.id):
                        row.status = "approved"
            else:
                listing.status = "rejected"
                if listing.renews_at is None:  # never went live: the first month is refunded
                    await self.svc.payment.refund(listing.last_payment_id)
            await self.svc.notifications.notify(
                listing.account_id, NotificationType.PAYMENT,
                "Your Wamiwa Agent plan is live" if approved else "Your Wamiwa Agent plan was not approved", reason)
        elif item.target_type == "promo_claim":
            claim = await self.repo(PromoClaim).require(item.target_id)
            if claim.status == "open":
                await self._decide_claim(claim, approved, reason)
        elif item.target_type == "promo_link":
            # approve = the link is safe; reject = it is a scam: remove it and, if asked, suspend the plan.
            link = await self.repo(PromoLink).get(item.target_id)
            if link is None:
                return
            listing = await self.repo(PromoListing).require(link.listing_id)
            if approved:
                # Remember what was judged safe, so the daily check only raises it again for a new reason.
                verdict = await self.providers.link_safety.check(link.url)
                link.cleared_reasons = "; ".join(verdict.reasons)[:300]
                link.flagged, link.flag_reason, link.reports = False, None, 0
            else:
                await self.repo(PromoLink).delete(link)
                if payload.get("suspend_plan"):
                    listing.status = "suspended"
            await self.svc.notifications.notify(
                listing.account_id, NotificationType.SECURITY,
                "Your link was checked and is being shared" if approved else "A link was removed from your plan", reason)
        else:
            row = await self.repo(PromoName).require(item.target_id)
            listing = await self.repo(PromoListing).require(row.listing_id)
            if approved and await self._claimed_by_other(row.normalized, listing.id):
                raise Conflict("Another verified account already holds this name", code="name_taken")
            row.status = "approved" if approved else "rejected"
            if approved and listing.kind in ("artist", "label"):
                await self._find_default(row)
            if "plain_text_match" in payload:
                row.plain_text_match = bool(payload["plain_text_match"])
            await self.svc.notifications.notify(
                listing.account_id, NotificationType.SECURITY,
                f"{row.name} is now linked to your plan" if approved else f"We could not confirm {row.name}", reason)
        await self._invalidate()

    # --- category keywords (admin) -----------------------------------------------------------------
    async def set_keywords(self, admin: Account, category: str, keywords: list[str], reason: str) -> dict[str, list[str]]:
        await self.svc.admin.require_permission(admin, Permission.B2B_CATEGORY_MAP)
        category = category.strip().lower().replace(" ", "_")
        words = sorted({k.strip().lower() for k in keywords if len(k.strip()) >= 3})
        for row in await self.repo(PromoCategoryKeyword).list(PromoCategoryKeyword.category == category):
            await self.repo(PromoCategoryKeyword).delete(row)
        for word in words:
            taken = await self.repo(PromoCategoryKeyword).first(PromoCategoryKeyword.keyword == word)
            if taken is not None:
                raise Conflict(f"'{word}' already points to {taken.category}", code="keyword_taken")
            await self.repo(PromoCategoryKeyword).add(PromoCategoryKeyword(category=category, keyword=word, updated_by=admin.id))
        await self.svc.audit.log(admin.id, "promotions.keywords", "category", category[:32], reason, keywords=words)
        await self._invalidate()
        return {category: words}

    async def keywords(self) -> dict[str, str]:
        async def load() -> dict[str, str]:
            return {row.keyword: row.category for row in await self.repo(PromoCategoryKeyword).list()}

        return await self.cache.get_or_set("promo:keywords", 300, load) or {}

    # --- the directory of live names, cached ---------------------------------------------------------
    async def _invalidate(self) -> None:
        await self.cache.invalidate_prefix("promo:")

    async def _directory(self) -> list[dict[str, Any]]:
        """Every approved name of every live plan. Small, read on every post and comment, so cached."""
        async def load() -> list[dict[str, Any]]:
            rows = (await self.db.execute(
                select(PromoName, PromoListing, Account)
                .join(PromoListing, PromoListing.id == PromoName.listing_id)
                .join(Account, Account.id == PromoListing.account_id)
                .where(PromoName.status == "approved", PromoListing.status == "active",
                       Account.status == AccountStatus.ACTIVE))).all()
            return [{"name_id": n.id, "normalized": n.normalized, "plain": n.plain_text_match, "primary": n.primary,
                     "listing_id": listing.id, "account_id": account.id, "handle": account.handle,
                     "kind": listing.kind, "category": listing.category, "region": account.region,
                     "target_regions": listing.target_regions or [],
                     "has_links": bool(await self._shareable(listing, n.id))}
                    for n, listing, account in rows]

        return await self.cache.get_or_set("promo:directory", 60, load) or []

    @staticmethod
    def _find(entry: dict[str, Any], text: str, handles: list[str]) -> tuple[int, int] | None:
        """Where this name is mentioned in the text, if it is."""
        if entry["primary"] and entry["handle"] in handles:
            position = text.lower().find("@" + entry["handle"])
            return position, position + len(entry["handle"]) + 1
        name = re.escape(entry["normalized"])
        if entry["plain"]:
            found = re.search(rf"(?<![\w@]){name}(?!\w)", text, re.I)
        else:
            quoted = rf"['\"“‘]({name})['\"”’]"
            found = re.search(rf"{CUE_WORDS}\s+['\"“‘]?({name})(?!\w)|{quoted}", text, re.I)
            if found:
                group = 1 if found.group(1) else 2
                return found.start(group), found.end(group)
        return (found.start(), found.end()) if found else None

    # --- deciding what a new post or comment gets ----------------------------------------------------
    async def scan(self, principal: Principal, post: Post, comment: Comment | None = None) -> list[PromoMatch]:
        """Called once when a post or comment is created. Applies every rule, then stores the result."""
        author = principal.account
        text = comment.text if comment else post.text
        item = comment or post
        if principal.is_machine or author.is_agent or author.is_admin or not text:
            return []  # only what people write in the app; never agents, assistants or API posts
        if item.moderation_status != ModerationStatus.VISIBLE or post.moderation_status != ModerationStatus.VISIBLE:
            return []
        owner = author if post.author_account_id == author.id else await self.repo(Account).require(post.author_account_id)
        if author.promo_opt_out or owner.promo_opt_out:
            return []
        if await self.svc.access.is_minor(author.id) or await self.svc.access.is_minor(owner.id):
            return []  # nothing sponsored on anything written by, or posted by, a 13-17 account
        if SENSITIVE.search(text) or COMPLAINT.search(text):
            return []
        sentiment = await self.providers.moderation.sentiment(text)
        if sentiment.label == "negative":
            return []

        only_account = await self._exclusive_account(post, owner)
        directory = await self._directory()
        handles = extract_mentions(text)
        blocked = await self.svc.blocks.hidden_ids(author.id) | await self.svc.blocks.hidden_ids(owner.id)
        matches: list[PromoMatch] = []
        seen: set[str] = set()
        for entry in directory:
            if entry["listing_id"] in seen or entry["account_id"] in blocked:
                continue
            if only_account is not None and entry["account_id"] != only_account:
                continue  # no competitor's card on a company's own post or on a sponsored post
            span = self._find(entry, text, handles)
            if span is None:
                continue
            seen.add(entry["listing_id"])
            matches.append(await self.repo(PromoMatch).add(PromoMatch(
                target_type="comment" if comment else "post", target_id=item.id, post_id=post.id,
                thread_id=comment.thread_id if comment else None, kind="mention",
                listing_id=entry["listing_id"], name_id=entry["name_id"], start=span[0], end=span[1])))
            if len(matches) >= self.s.promo_mentions_per_item:
                break

        if matches:
            if comment is not None and ASK.search(text):
                for match in matches:
                    entry = next(e for e in directory if e["name_id"] == match.name_id)
                    if entry["account_id"] != author.id:
                        await self._reply(comment, post, owner, author, match)
            return matches
        if only_account is None:
            suggestion = await self._suggestion(text, post, comment, blocked)
            if suggestion:
                matches.append(suggestion)
        return matches

    async def rescan(self, principal: Principal, post: Post, comment: Comment | None = None) -> list[PromoMatch]:
        """After an edit (FD-08): the old cards pointed at text that is gone, so they go and the new text is scanned."""
        await self.db.execute(delete(PromoMatch).where(
            PromoMatch.target_type == ("comment" if comment else "post"), PromoMatch.target_id == (comment or post).id))
        return await self.scan(principal, post, comment)

    async def _exclusive_account(self, post: Post, owner: Account) -> str | None:
        """Whose cards alone may appear under this post: the hiring brand on a sponsored post, or the
        company on its own post. None means any paying account may."""
        if post.sponsored_contract_id:
            contract = await self.repo(Contract).get(post.sponsored_contract_id)
            if contract:
                return contract.hirer_id
        return owner.id if owner.is_company else None

    async def _suggestion(self, text: str, post: Post, comment: Comment | None, blocked: set[str]) -> PromoMatch | None:
        """Someone wants a kind of product and named no brand: offer the "Suggested sellers" link."""
        if not WANT.search(text) or NOT_WANT.search(text):
            return None
        keywords = await self.keywords()
        lowered = text.lower()
        category = None
        for keyword in sorted(keywords, key=len, reverse=True):
            if re.search(rf"(?<!\w){re.escape(keyword)}(?:s|es)?(?!\w)", lowered):
                category = keywords[keyword]
                break
        if category is None:
            return None
        if not any(e for e in await self._sellers(category) if e["account_id"] not in blocked):
            return None  # an empty list is worse than no link
        # One suggestion per thread (or per post, for the post itself) keeps it from repeating.
        scope = [PromoMatch.post_id == post.id, PromoMatch.kind == "suggestion"]
        scope.append(PromoMatch.thread_id == comment.thread_id if comment else PromoMatch.target_type == "post")
        if await self.repo(PromoMatch).exists(*scope):
            return None
        return await self.repo(PromoMatch).add(PromoMatch(
            target_type="comment" if comment else "post", target_id=(comment or post).id, post_id=post.id,
            thread_id=comment.thread_id if comment else None, kind="suggestion", category=category))

    async def _sellers(self, category: str) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for entry in await self._directory():
            if entry["kind"] in LISTED_KINDS and entry["category"] == category and entry["primary"] and entry["has_links"]:
                seen.setdefault(entry["listing_id"], entry)
        return list(seen.values())

    async def _reply(self, comment: Comment, post: Post, owner: Account, author: Account, match: PromoMatch) -> None:
        """The Wamiwa Agent answers a question about a paying name: once per thread, clearly sponsored."""
        if owner.is_company:
            return
        owner_profile = await self.svc.access.profile(owner.id)
        agent = await self.ensure_agent()
        if owner_profile:
            policy = owner_profile.agent_comment_policy  # the creator's own rule for agent comments (FD-03)
            if policy == AgentCommentPolicy.NONE or (policy == AgentCommentPolicy.FOLLOWED and not
                                                     await self.svc.follows.is_following(owner.id, agent.id)):
                return
        if await self.repo(Comment).exists(Comment.thread_id == comment.thread_id,
                                           Comment.sponsored_listing_id == match.listing_id):
            return
        day = self.clock.today().isoformat()
        # Someone repeating a name to make the agent talk gets a handful of replies a day, no more.
        if await self.kv.incr(f"promo:bait:{author.id}:{day}", ttl=2 * 86400) > self.s.promo_author_triggers_per_day:
            return
        hour = int(self.clock.timestamp() // 3600)
        if await self.kv.incr(f"promo:agent:{hour}", ttl=7200) > self.s.wamiwa_agent_replies_per_hour:
            return
        listing = await self.repo(PromoListing).require(match.listing_id)
        name = await self.repo(PromoName).require(match.name_id)
        link = self._pick(await self._shareable(listing, name.id), comment.text)
        if link is None:
            return  # nothing to share: no link added and no default found
        # The reply carries Wamiwa's own address for the link, so a deleted link or a lapsed plan stops it working.
        await self.repo(Comment).add(Comment(
            post_id=post.id, parent_id=comment.id, thread_id=comment.thread_id, author_account_id=agent.id,
            label=Label.AGENT, text=f"{name.name}: {link.label}.", link=f"/promotions/links/{link.id}/click",
            system_generated=True, sponsored_listing_id=listing.id))
        await self.svc.comments.recount(post)
        await self._bump(listing.id, name.id, "agent_replies")

    # --- showing cards to a reader --------------------------------------------------------------------
    async def _audience(self, viewer: Account | None) -> bool:
        """Sponsored content is for adult humans only (section 20: minors' data is never used for ads)."""
        if viewer is None or viewer.is_agent or viewer.is_admin:
            return False
        return not await self.svc.access.is_minor(viewer.id)

    async def decorate(self, viewer: Account | None, items: list[dict[str, Any]], target_type: str) -> None:
        """Attach `promotions` and `suggested_sellers` to posts or comments about to be shown."""
        if not items or not await self._audience(viewer):
            return
        by_id = {item["id"]: item for item in items}
        matches = await self.repo(PromoMatch).list(
            PromoMatch.target_type == target_type, PromoMatch.target_id.in_(list(by_id)))
        if not matches:
            return
        live = {entry["name_id"]: entry for entry in await self._directory()}
        hidden = await self.svc.blocks.hidden_ids(viewer.id)
        for match in matches:
            item = by_id[match.target_id]
            if match.kind == "suggestion":
                if any(e["account_id"] not in hidden for e in await self._sellers(match.category)):
                    item["suggested_sellers"] = {
                        "label": "Suggested sellers", "sponsored": True, "category": match.category,
                        "url": f"/promotions/suggested?category={match.category}&source={match.id}"}
                continue
            entry = live.get(match.name_id)
            if entry is None or entry["account_id"] in hidden:
                continue  # plan lapsed, paused, account suspended, or the reader has blocked them
            item.setdefault("promotions", []).append({
                "type": "mention", "sponsored": True, "start": match.start, "end": match.end,
                "listing_id": match.listing_id, "card_url": f"/promotions/cards/{match.id}"})
            if entry["account_id"] != viewer.id and await self._first_today("imp", match.id, viewer.id):
                await self._bump(match.listing_id, match.name_id, "card_impressions")

    async def card(self, viewer: Account, match_id: str) -> dict[str, Any]:
        """The card behind a tappable name."""
        if not await self._audience(viewer):
            raise NotFound("Card not found")
        match = await self.repo(PromoMatch).get(match_id)
        if match is None or match.kind != "mention":
            raise NotFound("Card not found")
        return await self._card(viewer, match.listing_id, match.name_id, "card_opens", f"match={match.id}")

    async def _live(self, viewer: Account, listing_id: str | None, what: str) -> tuple[PromoListing, Account]:
        """The listing, only if it may be shown to this reader right now."""
        listing = await self.repo(PromoListing).get(listing_id)
        account = await self.repo(Account).get(listing.account_id) if listing else None
        if listing is None or listing.status != "active" or account is None or account.status != AccountStatus.ACTIVE \
                or await self.svc.blocks.either(viewer.id, account.id):
            raise NotFound(f"{what} not found")
        return listing, account

    async def _card(self, viewer: Account, listing_id: str, name_id: str | None, stat: str, source: str) -> dict[str, Any]:
        listing, account = await self._live(viewer, listing_id, "Card")
        name = await self.repo(PromoName).get(name_id) if name_id else await self.repo(PromoName).first(
            PromoName.listing_id == listing.id, PromoName.primary.is_(True))
        if name is None or name.status != "approved":
            raise NotFound("Card not found")
        if account.id != viewer.id and await self._first_today(stat, f"{listing.id}:{name.id}", viewer.id):
            await self._bump(listing.id, name.id if stat == "card_opens" else None, stat)
        return {
            "sponsored": True, "name": name.name, "kind": listing.kind, "category": listing.category,
            "profile": {"id": account.id, "handle": account.handle},
            "links": [{"label": link.label, "click_url": f"/promotions/links/{link.id}/click",
                       "report_url": None if link.is_default else f"/promotions/links/{link.id}/report"}
                      for link in await self._shareable(listing, name.id)],
        }

    async def click_link(self, viewer: Account, link_id: str) -> dict[str, Any]:
        """Records the tap and hands back the real address. Fails once the link is deleted, falls outside
        the package's limit, is under review as a suspected scam, or the plan is no longer active."""
        if not await self._audience(viewer):
            raise NotFound("Link not found")
        if link_id.startswith(DEFAULT_PREFIX):
            name = await self.repo(PromoName).get(link_id[len(DEFAULT_PREFIX):])
            listing_id, name_id = (name.listing_id, name.id) if name else (None, None)
        else:
            link = await self.repo(PromoLink).get(link_id)
            if link is not None and link.flagged:
                raise Gone("This link was reported as unsafe and has been switched off while we check it.",
                           code="link_under_review")
            listing_id, name_id = (link.listing_id, link.name_id) if link else (None, None)
        if listing_id is None:
            raise NotFound("Link not found")
        listing, account = await self._live(viewer, listing_id, "Link")
        shared = next((l for l in await self._shareable(listing, name_id) if l.id == link_id), None)
        if shared is None:
            raise NotFound("Link not found")
        if account.id != viewer.id and await self._first_today("link_clicks", link_id, viewer.id):
            await self._bump(listing.id, shared.name_id if listing.kind == "label" or shared.is_default else None,
                             "link_clicks")
        return {"url": shared.url, "label": shared.label}

    async def click_listing(self, viewer: Account, listing_id: str) -> dict[str, Any]:
        """A tap on a seller in the Suggested sellers list: opens one of their links, chosen at random."""
        if not await self._audience(viewer):
            raise NotFound("Link not found")
        listing, account = await self._live(viewer, listing_id, "Link")
        link = self._pick(await self._shareable(listing, None), "")
        if link is None:
            raise NotFound("Link not found")
        if account.id != viewer.id and await self._first_today("list_clicks", listing.id, viewer.id):
            await self._bump(listing.id, None, "list_clicks")
        return {"url": link.url, "label": link.label}

    async def suggested(self, viewer: Account, category: str, limit: int) -> dict[str, Any]:
        """The Suggested sellers list: the reader's region first, the rest rotated so nobody is always on top."""
        if not await self._audience(viewer):
            return {"category": category, "sponsored": True, "sellers": [], "has_more": False}
        hidden = await self.svc.blocks.hidden_ids(viewer.id)
        day = self.clock.today().isoformat()
        sellers = [e for e in await self._sellers(category.strip().lower()) if e["account_id"] not in hidden]
        sellers.sort(key=lambda e: (
            0 if viewer.region and e["region"] == viewer.region else 1,
            hashlib.sha256(f"{e['listing_id']}:{viewer.id}:{day}".encode()).hexdigest()))
        limit = max(1, min(limit or self.s.promo_suggestions_shown, 20))
        shown = sellers[:limit]
        names = await self.repo(PromoName).by_ids([e["name_id"] for e in shown])
        accounts = await self.repo(Account).by_ids([e["account_id"] for e in shown])
        result = []
        for entry in shown:
            if entry["account_id"] != viewer.id and await self._first_today("list_impressions", entry["listing_id"], viewer.id):
                await self._bump(entry["listing_id"], None, "list_impressions")
            result.append({
                "name": names[entry["name_id"]].name, "kind": entry["kind"],
                "profile": {"id": entry["account_id"], "handle": accounts[entry["account_id"]].handle},
                "in_your_region": bool(viewer.region and entry["region"] == viewer.region),
                "click_url": f"/promotions/listings/{entry['listing_id']}/click"})
        return {"category": category, "sponsored": True, "sellers": result, "has_more": len(sellers) > limit}

    # --- counting ------------------------------------------------------------------------------------------
    async def _first_today(self, what: str, subject: str, viewer_id: str) -> bool:
        """Count each person once a day per card, so refreshing or tapping repeatedly cannot inflate numbers."""
        day = self.clock.today().isoformat()
        return await self.kv.set_if_absent(f"promo:seen:{what}:{subject}:{viewer_id}:{day}", "1", 2 * 86400)

    async def _bump(self, listing_id: str, name_id: str | None, field: str) -> None:
        day, name_key = self.clock.today(), name_id or ""
        column = getattr(PromoDailyStat, field)
        result = await self.db.execute(
            update(PromoDailyStat).where(PromoDailyStat.listing_id == listing_id, PromoDailyStat.name_id == name_key,
                                         PromoDailyStat.day == day).values({column: column + 1})
            .execution_options(synchronize_session=False))
        if not result.rowcount:
            await self.repo(PromoDailyStat).add(PromoDailyStat(
                listing_id=listing_id, name_id=name_key, day=day, **{field: 1}))

    async def analytics(self, account: Account, days: int) -> dict[str, Any] | None:
        """What the plan's owner sees under Analytics."""
        listing = await self.repo(PromoListing).first(PromoListing.account_id == account.id)
        if listing is None:
            return None
        since = self.clock.today() - timedelta(days=max(1, min(days, 365)))
        rows = await self.repo(PromoDailyStat).list(
            PromoDailyStat.listing_id == listing.id, PromoDailyStat.day >= since, order_by=PromoDailyStat.day)
        names = {n.id: n.name for n in await self.repo(PromoName).list(PromoName.listing_id == listing.id)}
        totals = dict.fromkeys(STAT_FIELDS, 0)
        by_day: dict[str, dict[str, int]] = {}
        by_name: dict[str, dict[str, int]] = {}
        for row in rows:
            day = by_day.setdefault(row.day.isoformat(), dict.fromkeys(STAT_FIELDS, 0))
            name = by_name.setdefault(names.get(row.name_id, "Suggested sellers list"), dict.fromkeys(STAT_FIELDS, 0))
            for field in STAT_FIELDS:
                value = getattr(row, field)
                totals[field] += value
                day[field] += value
                name[field] += value

        def rate(part: int, whole: int) -> float:
            return round(part / whole * 100, 2) if whole else 0.0

        return {
            "plan": {"kind": listing.kind, "tier": listing.tier, "status": listing.status,
                     "renews_at": listing.renews_at.isoformat() if listing.renews_at else None},
            "totals": totals,
            "rates": {"card_open_rate_pct": rate(totals["card_opens"], totals["card_impressions"]),
                      "card_click_rate_pct": rate(totals["link_clicks"], totals["card_opens"]),
                      "list_click_rate_pct": rate(totals["list_clicks"], totals["list_impressions"])},
            "by_day": [{"day": day, **values} for day, values in by_day.items()],
            "by_name": [{"name": name, **values} for name, values in by_name.items()],
            "note": "Counts adult human readers only, each at most once a day per card. Your own views are left out.",
        }

    # --- admin monitoring ---------------------------------------------------------------------------------
    async def admin_overview(self, admin: Account, days: int) -> dict[str, Any]:
        """The Wamiwa Agent dashboard: open to every Admin; money figures only with the revenue permission."""
        since = self.clock.today() - timedelta(days=max(1, min(days, 365)))
        plans = (await self.db.execute(
            select(PromoListing.kind, PromoListing.tier, PromoListing.status, func.count())
            .group_by(PromoListing.kind, PromoListing.tier, PromoListing.status))).all()
        by_status: dict[str, int] = {}
        active: dict[str, int] = {}
        mrr_cents = 0
        for kind, tier, status, count in plans:
            by_status[status] = by_status.get(status, 0) + count
            if status == "active":
                active[f"{kind}:{tier}"] = count
                mrr_cents += self.price_cents(kind, tier) * count
        totals = (await self.db.execute(select(*[func.coalesce(func.sum(getattr(PromoDailyStat, f)), 0) for f in STAT_FIELDS])
                                        .where(PromoDailyStat.day >= since))).one()
        data: dict[str, Any] = {
            "period_days": days, "plans_by_status": by_status, "active_plans": active,
            "activity": dict(zip(STAT_FIELDS, (int(v) for v in totals))),
            "names_live": await self.repo(PromoName).count(PromoName.status == "approved"),
            "links_saved": await self.repo(PromoLink).count(),
            "links_under_review": await self.repo(PromoLink).count(PromoLink.flagged.is_(True)),
            "waiting_for_review": await self.svc.queues.open_count(Queue.PROMOTIONS),
        }
        if await self.svc.admin.has_permission(admin, Permission.VIEW_REVENUE):
            from app.models import Payment

            data["revenue"] = {
                "monthly_recurring_usd": round(mrr_cents / 100, 2), "currency": self.s.local_currency,
                "collected_in_period": await self.repo(Payment).sum(
                    Payment.amount_local - Payment.refunded_local, Payment.purpose == PaymentPurpose.PROMOTION,
                    Payment.created_at >= self.now() - timedelta(days=days))}
        return data

    async def admin_list(self, *, status: str | None, kind: str | None, under_review: bool, cursor: str | None,
                         limit: int):
        where = []
        if status:
            where.append(PromoListing.status == status)
        if kind:
            where.append(PromoListing.kind == kind)
        if under_review:
            where.append(PromoListing.id.in_(select(PromoLink.listing_id).where(PromoLink.flagged.is_(True))))
        page = await self.repo(PromoListing).page(*where, cursor=cursor, limit=limit)
        accounts = await self.repo(Account).by_ids([l.account_id for l in page.items])
        page.items = [{"id": l.id, "account_id": l.account_id, "handle": accounts[l.account_id].handle,
                       "kind": l.kind, "tier": l.tier, "status": l.status, "category": l.category,
                       "renews_at": l.renews_at.isoformat() if l.renews_at else None} for l in page.items]
        return page

    async def admin_detail(self, listing_id: str) -> dict[str, Any]:
        listing = await self.repo(PromoListing).require(listing_id, "Plan not found")
        account = await self.repo(Account).require(listing.account_id)
        return {"plan": await self.mine(account), "owner": {"id": account.id, "handle": account.handle, "status": account.status},
                "results": await self.analytics(account, 30)}

    async def admin_set_suspended(self, admin: Account, listing_id: str, suspended: bool, reason: str) -> dict[str, Any]:
        """Switch a plan off (or back on). Logged with a reason like every other admin action."""
        listing = await self.repo(PromoListing).require(listing_id, "Plan not found")
        await self.svc.admin.require_permission(admin, Permission.SUSPEND)
        await self.svc.audit.log(admin.id, "promotions.suspend" if suspended else "promotions.restore",
                                 "promo_listing", listing.id, reason)
        if suspended and listing.status in ("active", "paused", "pending_review"):
            listing.status = "suspended"
        elif not suspended and listing.status == "suspended":
            listing.status = "active" if listing.renews_at and listing.renews_at > self.now() else "lapsed"
        await self.svc.notifications.notify(
            listing.account_id, NotificationType.LEGAL,
            "Your Wamiwa Agent plan was suspended" if suspended else "Your Wamiwa Agent plan was restored", reason)
        await self._invalidate()
        return await self.admin_detail(listing.id)

    async def admin_remove_link(self, admin: Account, link_id: str, reason: str) -> None:
        link = await self.repo(PromoLink).require(link_id, "Link not found")
        listing = await self.repo(PromoListing).require(link.listing_id)
        await self.svc.audit.log(admin.id, "promotions.remove_link", "promo_link", link.id, reason, url=link.url)
        await self.repo(PromoLink).delete(link)
        await self.svc.queues.close_for_target(Queue.PROMOTIONS, link.id, "reject", admin.id, reason)
        await self.svc.notifications.notify(listing.account_id, NotificationType.SECURITY,
                                            "A link was removed from your plan", reason)
        await self._invalidate()

    # --- Plus package: content ideas from what is likely to trend ---------------------------------------
    async def content_ideas(self, account: Account, region: str | None) -> dict[str, Any]:
        """Ideas for the regions the owner targets, which need not be where they are (someone in Nigeria
        can target China or the US). Two kinds of input are combined:

          * science: what is measurably rising - engagement on Wamiwa in that region over the last week,
            and an outside trends source;
          * art: how to say it - the region's upcoming cultural moments and a creative format to carry it.

        These are informed suggestions, not predictions.
        """
        listing = await self._listing(account)
        if listing.tier != "plus" or listing.status != "active":
            raise Forbidden("Content ideas come with the Plus package", code="plus_required",
                            hint="PATCH /promotions with {\"tier\": \"plus\"}")
        regions = core_regions.require_many([region], 1) if region else (listing.target_regions or ([account.region.lower()] if account.region else []))
        if not regions:
            raise Unprocessable("Choose the regions you want to reach", code="regions_required",
                                hint="PATCH /promotions with target_regions")
        day = self.clock.today()

        async def load() -> dict[str, Any]:
            return {"day": day.isoformat(), "regions": [await self._ideas_for(listing, r) for r in regions[: self.s.promo_max_target_regions]],
                    "note": "Suggestions built from engagement data and cultural timing. They are not guarantees."}

        return await self.cache.get_or_set(f"trendideas:{listing.id}:{day.isoformat()}:{','.join(regions)}", 6 * 3600, load)

    async def _ideas_for(self, listing: PromoListing, region: str) -> dict[str, Any]:
        return await self.ideas_for_topic("music" if listing.kind in ("artist", "label") else (listing.category or ""), region)

    async def ideas_for_topic(self, topic_area: str, region: str) -> dict[str, Any]:
        """Trend-based ideas for one topic area in one region (WA-10 for plans, SB-03 for Pro creators)."""
        signals: dict[str, dict[str, Any]] = {}

        # Science 1: what people in that region engaged with on Wamiwa this week, weighted towards the newest.
        since = self.now() - timedelta(days=7)
        rows = (await self.db.execute(
            select(Post.text, Post.niche, Post.likes_human, Post.comments_human, Post.impressions_human, Post.created_at)
            .join(Account, Account.id == Post.author_account_id)
            .where(func.lower(Account.region) == region, Post.created_at >= since, Post.label == Label.HUMAN,
                   Post.moderation_status == ModerationStatus.VISIBLE).limit(2000))).all()
        for text, niche, likes, comments, impressions, created in rows:
            age_days = max(0.0, (self.now() - created).total_seconds() / 86400)
            weight = (likes + 2 * comments + impressions * 0.05) * (0.5 ** (age_days / 3)) * (1.5 if niche == topic_area else 1)
            for term in set(re.findall(r"#(\w{3,30})", (text or "").lower())) | ({niche} if niche else set()):
                entry = signals.setdefault(term, {"score": 0.0, "sources": set()})
                entry["score"] += weight
                entry["sources"].add("engagement on Wamiwa")
        top = max((e["score"] for e in signals.values()), default=0.0)
        for entry in signals.values():
            entry["score"] = entry["score"] / top if top else 0.0

        # Science 2: an outside trends source for that region.
        for signal in await self.providers.trends.rising(region, topic_area or None):
            entry = signals.setdefault(signal.topic.lower(), {"score": 0.0, "sources": set()})
            entry["score"] = min(1.0, entry["score"] + signal.score)
            entry["sources"].add(signal.source)

        # Art: the moment and the form.
        moments = [m for m in CULTURAL_MOMENTS.get(region, []) + CULTURAL_MOMENTS["*"]
                   if 0 <= self._days_until(m[0], m[1]) <= 30]
        ranked = sorted(signals.items(), key=lambda item: item[1]["score"], reverse=True)[:5]
        ideas = []
        for index, (topic, entry) in enumerate(ranked):
            angle = CREATIVE_FORMATS[(index + self.clock.today().toordinal()) % len(CREATIVE_FORMATS)]
            moment = moments[index % len(moments)] if moments else None
            idea = angle.format(topic=topic, area=topic_area or "your work")
            if moment:
                idea += f" Tie it to {moment[2]}, {self._days_until(moment[0], moment[1])} days away."
            ideas.append({
                "idea": idea, "topic": topic,
                "confidence": "high" if entry["score"] >= 0.7 and len(entry["sources"]) > 1 else
                              "medium" if entry["score"] >= 0.4 else "low",
                "why": sorted(entry["sources"]) + ([f"upcoming: {moment[2]}"] if moment else [])})
        if not ideas:  # nothing measurable yet: fall back to timing alone, and say so
            for moment in moments[:3]:
                ideas.append({"idea": f"Make something for {moment[2]} that shows {topic_area or 'what you do'} in a "
                                      f"way only you can. It is {self._days_until(moment[0], moment[1])} days away.",
                              "topic": moment[2], "confidence": "low", "why": [f"upcoming: {moment[2]}"]})
        return {"region": region, "ideas": ideas,
                "based_on": {"posts_analysed": len(rows), "upcoming_moments": [m[2] for m in moments]}}

    def _days_until(self, month: int, day: int) -> int:
        today = self.clock.today()
        target = today.replace(month=month, day=day)
        if target < today:
            target = target.replace(year=today.year + 1)
        return (target - today).days
