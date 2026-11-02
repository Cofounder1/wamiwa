"""SB-03 Pro plan and FD-09 "For you": what someone is about, the businesses that fit them, and what to post next.

A person's interests come from two places: the niches they chose, and what they actually post about
(hashtags and topic words, newest weighted most). Someone who posts about #ml every week is matched
with tech businesses and ML posts even if they only chose "Tech" as a niche.
"""
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.enums import AccountStatus, AccountType, ContractType, Label, ModerationStatus
from app.core import regions as core_regions
from app.core.exceptions import Unprocessable
from app.models import Account, CompanyProfile, Contract, Post, Subscription
from app.services.base import BaseService

# Starting topic words per category, so matching works before admins add their own keyword lists
# (those, from the Wamiwa Agent keyword admin, are added on top).
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "fashion": ["fashion", "outfit", "ootd", "style", "streetwear", "thrift", "ankara", "aso ebi", "sneakers",
                "dress", "shirt", "menswear", "womenswear", "tailor", "fabric", "lookbook"],
    "beauty": ["beauty", "makeup", "skincare", "skin care", "hair", "wig", "braids", "nails", "lipstick",
               "foundation", "glow", "fragrance", "perfume"],
    "food & drinks": ["food", "recipe", "cooking", "jollof", "suya", "restaurant", "chef", "baking", "foodie",
                      "drinks", "cocktail", "coffee", "street food"],
    "music": ["music", "afrobeats", "amapiano", "song", "album", "producer", "beat", "concert", "dj", "rap",
              "gospel", "singer"],
    "tech": ["tech", "technology", "ai", "ml", "machine learning", "deep learning", "data science", "llm",
             "software", "coding", "programming", "developer", "python", "javascript", "startup", "saas",
             "cloud", "cybersecurity", "gadget", "smartphone", "app", "web3", "robotics"],
    "finance": ["finance", "money", "investing", "investment", "stocks", "savings", "budget", "fintech",
                "banking", "crypto", "personal finance", "trading"],
    "fitness": ["fitness", "gym", "workout", "training", "running", "yoga", "pilates", "protein", "wellness",
                "health"],
    "travel": ["travel", "trip", "vacation", "holiday", "flight", "hotel", "tourism", "backpacking", "detty december"],
    "gaming": ["gaming", "gamer", "esports", "playstation", "xbox", "nintendo", "fifa", "fortnite", "twitch"],
    "comedy": ["comedy", "skit", "funny", "joke", "standup", "stand-up", "meme"],
    "art & design": ["art", "design", "illustration", "painting", "drawing", "graphic design", "ui", "ux",
                     "photography", "3d"],
    "education": ["education", "learning", "study", "exam", "university", "school", "course", "tutorial",
                  "scholarship", "teacher"],
    "parenting": ["parenting", "mum", "mom", "dad", "baby", "kids", "pregnancy", "motherhood"],
    "sports": ["sports", "football", "soccer", "basketball", "athletics", "boxing", "premier league", "afcon"],
    "lifestyle": ["lifestyle", "vlog", "day in my life", "home", "decor", "self care", "productivity"],
    "autos": ["car", "cars", "auto", "autos", "motor", "vehicle", "ev", "electric vehicle", "bike"],
    "agriculture": ["agriculture", "farming", "farm", "agritech", "poultry", "crops", "livestock"],
}

HASHTAG = re.compile(r"#(\w{2,40})")
WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
POST_WINDOW_DAYS = 60
HIRING_WINDOW_DAYS = 30


def _category(value: str | None) -> str | None:
    return (value or "").strip().lower() or None


class InterestService(BaseService):
    async def keyword_map(self) -> dict[str, str]:
        mapping = {word: category for category, words in TOPIC_KEYWORDS.items() for word in words}
        mapping.update({k.lower(): _category(v) for k, v in (await self.svc.promos.keywords()).items() if v})
        mapping.update({k.replace(" ", ""): v for k, v in list(mapping.items()) if " " in k})  # #machinelearning
        return mapping

    async def read(self, text: str, keyword_map: dict[str, str] | None = None) -> tuple[set[str], set[str]]:
        """The topic terms and categories a piece of text is about: its hashtags, plus any known topic
        words or phrases (up to three words, so "machine learning" is found as well as "ml")."""
        keyword_map = keyword_map if keyword_map is not None else await self.keyword_map()
        lowered = (text or "").lower()
        terms = set(HASHTAG.findall(lowered))
        words = WORD.findall(lowered)
        phrases = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
        categories: set[str] = set()
        for phrase in phrases | terms:
            category = keyword_map.get(phrase) or keyword_map.get(phrase.replace("_", " "))
            if category:
                terms.add(phrase.replace(" ", ""))
                categories.add(category)
        return terms, categories

    def category_of(self, company_category: str | None, keyword_map: dict[str, str]) -> str | None:
        """A company's category in the same vocabulary as niches ("fintech" counts as finance)."""
        category = _category(company_category)
        if category is None:
            return None
        if category in TOPIC_KEYWORDS:
            return category
        return keyword_map.get(category, category)

    async def profile(self, account: Account) -> dict[str, Any]:
        """Weighted topics and categories for one person. Cached for an hour."""
        async def load() -> dict[str, Any]:
            terms: dict[str, float] = {}
            categories: dict[str, float] = {}
            profile = await self.svc.access.profile(account.id)
            if profile and profile.primary_niche:
                categories[_category(profile.primary_niche)] = 3.0
            for niche in (profile.secondary_niches if profile else None) or []:
                key = _category(niche)
                categories[key] = categories.get(key, 0) + 2.0
            since = self.now() - timedelta(days=POST_WINDOW_DAYS)
            posts = await self.repo(Post).list(
                Post.author_account_id == account.id, Post.created_at >= since,
                Post.moderation_status == ModerationStatus.VISIBLE, order_by=Post.id.desc(), limit=200)
            keyword_map = await self.keyword_map()
            for post in posts:
                weight = 0.5 ** ((self.now() - post.created_at).days / 14)  # what they post now counts most
                post_terms, post_categories = await self.read(post.text, keyword_map)
                for term in post_terms:
                    terms[term] = terms.get(term, 0) + weight
                for category in post_categories:
                    categories[category] = categories.get(category, 0) + weight
            top = lambda d, n: dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n])  # noqa: E731
            return {"terms": top(terms, 30), "categories": top(categories, 6), "posts_read": len(posts)}

        return await self.cache.get_or_set(f"interests:{account.id}", 3600, load)


class GrowthService(BaseService):
    # --- access ----------------------------------------------------------------------------------
    async def _pro(self, account: Account, feature: str) -> Subscription:
        await self.svc.access.require_pro(account, feature)
        return await self.repo(Subscription).first(Subscription.account_id == account.id)

    async def _is_minor(self, account: Account) -> bool:
        return await self.svc.access.is_minor(account.id)

    # --- SB-03 partner suggestions -----------------------------------------------------------------
    async def partners(self, account: Account) -> dict[str, Any]:
        """Up to 50 verified businesses that fit what this person is about, with the reasons."""
        await self._pro(account, "Partner suggestions")
        interests = await self.svc.interests.profile(account)
        if not interests["terms"] and not interests["categories"]:
            return {"partners": [], "based_on": interests,
                    "hint": "Choose your niches in Settings, or post about what you do, to get suggestions."}

        async def load() -> list[dict[str, Any]]:
            keyword_map = await self.svc.interests.keyword_map()
            minor = await self._is_minor(account)
            hidden = await self.svc.blocks.hidden_ids(account.id)
            rows = (await self.db.execute(
                select(Account, CompanyProfile).join(CompanyProfile, CompanyProfile.account_id == Account.id)
                .where(Account.type == AccountType.COMPANY, Account.status == AccountStatus.ACTIVE,
                       CompanyProfile.verified.is_(True)).limit(3000))).all()
            companies = {a.id: (a, c) for a, c in rows if a.id not in hidden and not (
                minor and self.svc.access.is_blocked_category_for_minors(c.category))}
            if not companies:
                return []
            since = self.now() - timedelta(days=POST_WINDOW_DAYS)
            posts = (await self.db.execute(
                select(Post.author_account_id, Post.text).where(
                    Post.author_account_id.in_(list(companies)), Post.created_at >= since,
                    Post.moderation_status == ModerationStatus.VISIBLE).limit(5000))).all()
            company_terms: dict[str, dict[str, int]] = {}
            for author_id, text in posts:
                post_terms, _ = await self.svc.interests.read(text, keyword_map)
                bucket = company_terms.setdefault(author_id, {})
                for term in post_terms:
                    bucket[term] = bucket.get(term, 0) + 1
            hiring = {row[0] for row in (await self.db.execute(
                select(Contract.hirer_id).where(
                    Contract.hirer_id.in_(list(companies)),
                    Contract.type.in_([ContractType.HIRE_ME, ContractType.LIKENESS]),
                    Contract.created_at >= self.now() - timedelta(days=HIRING_WINDOW_DAYS)))).all()}

            results = []
            for company_id, (acc, company) in companies.items():
                score, reasons = 0.0, []
                category = self.svc.interests.category_of(company.category, keyword_map)
                if category and category in interests["categories"]:
                    score += 2 + interests["categories"][category]
                    reasons.append(f"{(company.category or category).title()} business, matching your niche")
                shared = sorted((t for t in company_terms.get(company_id, {}) if t in interests["terms"]),
                                key=lambda t: interests["terms"][t], reverse=True)
                if shared:
                    score += sum(interests["terms"][t] for t in shared) + len(shared)
                    reasons.append("Posts about " + ", ".join(f"#{t}" for t in shared[:3]))
                if score <= 0:
                    continue
                if company_id in hiring:
                    score += 1.5
                    reasons.append("Hiring creators now")
                results.append({
                    "id": acc.id, "handle": acc.handle, "name": company.business_name or acc.display_name,
                    "category": company.category, "verified": True, "hiring": company_id in hiring,
                    "accepts_messages": company.allow_user_messages, "score": round(score, 2), "why": reasons})
            results.sort(key=lambda r: (r["score"], r["hiring"]), reverse=True)
            return results[: self.s.growth_partner_suggestions]

        partners = await self.cache.get_or_set(f"partners:{account.id}", 3600, load)
        return {"partners": partners, "based_on": {"topics": list(interests["terms"])[:10],
                                                   "categories": list(interests["categories"])}}

    # --- SB-03 content ideas for chosen regions ------------------------------------------------------
    async def set_regions(self, account: Account, regions: list[str]) -> dict[str, Any]:
        subscription = await self._pro(account, "Region content ideas")
        subscription.target_regions = core_regions.require_many(regions, self.s.promo_max_target_regions)
        return {"target_regions": subscription.target_regions}

    async def ideas(self, account: Account, region: str | None) -> dict[str, Any]:
        subscription = await self._pro(account, "Region content ideas")
        regions = core_regions.require_many([region], 1) if region else (
            subscription.target_regions or ([account.region] if account.region else []))
        if not regions:
            raise Unprocessable("Choose the regions you want to reach", code="regions_required",
                                hint="PUT /growth/regions")
        interests = await self.svc.interests.profile(account)
        topic_area = next(iter(interests["categories"]), None) or ""
        day = self.clock.today()

        async def load() -> dict[str, Any]:
            return {"day": day.isoformat(), "topic_area": topic_area or None,
                    "regions": [await self.svc.promos.ideas_for_topic(topic_area, r) for r in regions[: self.s.promo_max_target_regions]],
                    "note": "Suggestions built from engagement data and cultural timing. They are not guarantees."}

        return await self.cache.get_or_set(f"growthideas:{account.id}:{day.isoformat()}:{','.join(regions)}", 6 * 3600, load)

    # --- FD-09 "For you": posts in your topics, for everyone ----------------------------------------
    async def for_you(self, viewer: Account, limit: int = 20) -> dict[str, Any]:
        interests = await self.svc.interests.profile(viewer)
        if not interests["terms"] and not interests["categories"]:
            return {"items": [], "based_on": {"topics": [], "categories": []},
                    "hint": "Choose your niches, or post about what you do, to see posts in your topics."}
        keyword_map = await self.svc.interests.keyword_map()
        minor = await self._is_minor(viewer)
        hidden = await self.svc.blocks.hidden_ids(viewer.id)
        since = self.now() - timedelta(days=14)
        rows = (await self.db.execute(
            select(Post, Account).join(Account, Account.id == Post.author_account_id)
            .where(Post.created_at >= since, Post.moderation_status == ModerationStatus.VISIBLE,
                   Post.label.in_([Label.HUMAN, Label.SPONSORED]), Account.status == AccountStatus.ACTIVE,
                   Post.author_account_id != viewer.id)
            .order_by(Post.id.desc()).limit(600))).all()
        companies = {c.account_id: c for c in await self.repo(CompanyProfile).list(
            CompanyProfile.account_id.in_({a.id for _, a in rows if a.type == AccountType.COMPANY}))}
        scored = []
        for post, author in rows:
            if author.id in hidden:
                continue
            company = companies.get(author.id)
            if minor and company and self.svc.access.is_blocked_category_for_minors(company.category):
                continue
            post_terms, post_categories = await self.svc.interests.read(post.text, keyword_map)
            company_category = self.svc.interests.category_of(company.category, keyword_map) if company else None
            if company_category:
                post_categories.add(company_category)
            shared_terms = [t for t in post_terms if t in interests["terms"]]
            shared_categories = [c for c in post_categories if c in interests["categories"]]
            if not shared_terms and not shared_categories:
                continue
            match = sum(interests["terms"][t] for t in shared_terms) + sum(interests["categories"][c] for c in shared_categories)
            if company:
                match *= 1.5  # businesses come first: they are who you can work with
                if company_category in interests["categories"]:
                    match *= 1.5  # and a business whose whole line is your niche, before one that mentions it
            age_days = (self.now() - post.created_at).total_seconds() / 86400
            why = (f"#{shared_terms[0]}" if shared_terms else shared_categories[0].title())
            scored.append((match * 0.5 ** (age_days / 3), post, author, why))
        scored.sort(key=lambda s: s[0], reverse=True)
        items = []
        for _, post, author, why in scored[: max(1, min(limit, 50))]:
            item = self.svc.posts.present(post, author)
            item["why"] = f"Because you post about {why}" if why.startswith("#") else f"From your niche: {why}"
            item["from_business"] = author.type == AccountType.COMPANY
            items.append(item)
        await self.svc.feed.record_impressions([i["id"] for i in items], viewer)
        await self.svc.posts.for_viewer(viewer, items)
        await self.svc.promos.decorate(viewer, items, "post")
        return {"items": items, "based_on": {"topics": list(interests["terms"])[:10],
                                             "categories": list(interests["categories"])}}
