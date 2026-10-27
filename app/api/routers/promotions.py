"""Wamiwa Agent (Epic 15): plans, names, cards, suggested sellers and the admin keyword map."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import Active, Admin, Services, ok, ok_page

router = APIRouter(tags=["wamiwa-agent"])


class LinkIn(BaseModel):
    url: str = Field(min_length=10, max_length=500)
    label: str = Field(min_length=2, max_length=60, description='What it is, e.g. "Spotify", "New single", "Shop"')
    keywords: list[str] = Field(default_factory=list, max_length=10, description="Words that make this the link to share")


class AddLinkIn(LinkIn):
    name_id: str | None = Field(default=None, description="Labels: the artist this link is for")


class SubscribeIn(BaseModel):
    kind: Literal["business", "seller", "artist", "label"]
    tier: Literal["base", "plus"] = "base"
    category: str | None = Field(default=None, max_length=64)
    brand_name: str | None = Field(default=None, max_length=80, description="Sellers: shop name. Artists: stage name")
    links: list[LinkIn] = Field(default_factory=list, max_length=10)
    target_regions: list[str] = Field(default_factory=list, max_length=5, description="Where you want to be seen")
    payment_method: str = Field(min_length=3, max_length=64)


class UpdateIn(BaseModel):
    category: str | None = Field(default=None, max_length=64)
    paused: bool | None = None
    tier: Literal["base", "plus"] | None = None
    target_regions: list[str] | None = Field(default=None, max_length=5)
    payment_method: str | None = Field(default=None, max_length=64)


class NameIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class ClaimIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    evidence: str = Field(default="", max_length=1000, description="Why this name is yours")


class ReportLinkIn(BaseModel):
    details: str = Field(default="", max_length=1000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class KeywordsIn(BaseModel):
    category: str = Field(min_length=2, max_length=64)
    keywords: list[str] = Field(max_length=200)
    reason: str = Field(min_length=3, max_length=2000)


@router.get("/promotions/plans")
async def plans(p: Active, svc: Services):
    return ok(await svc.promos.plans(p.account))


@router.get("/promotions")
async def my_plan(p: Active, svc: Services):
    return ok(await svc.promos.mine(p.account))


@router.post("/promotions", status_code=201)
async def subscribe(body: SubscribeIn, p: Active, svc: Services):
    return ok(await svc.promos.subscribe(p.account, **body.model_dump()))


@router.patch("/promotions")
async def update_plan(body: UpdateIn, p: Active, svc: Services):
    return ok(await svc.promos.update(p.account, body.model_dump(exclude_unset=True)))


@router.delete("/promotions")
async def cancel_plan(p: Active, svc: Services):
    return ok(await svc.promos.cancel(p.account))


@router.post("/promotions/names", status_code=201)
async def add_name(body: NameIn, p: Active, svc: Services):
    return ok(await svc.promos.add_name(p.account, body.name))


@router.post("/promotions/names/claim", status_code=201)
async def claim_name(body: ClaimIn, p: Active, svc: Services):
    return ok(await svc.promos.claim_name(p.account, body.name, body.evidence))


@router.delete("/promotions/names/{name_id}")
async def remove_name(name_id: str, p: Active, svc: Services):
    return ok(await svc.promos.remove_name(p.account, name_id))


@router.post("/promotions/links", status_code=201)
async def add_link(body: AddLinkIn, p: Active, svc: Services):
    return ok(await svc.promos.add_link(p.account, body.url, body.label, body.keywords, body.name_id))


@router.delete("/promotions/links/{link_id}")
async def remove_link(link_id: str, p: Active, svc: Services):
    return ok(await svc.promos.remove_link(p.account, link_id))


@router.post("/promotions/links/{link_id}/click")
async def open_link(link_id: str, p: Active, svc: Services):
    return ok(await svc.promos.click_link(p.account, link_id))


@router.post("/promotions/links/{link_id}/report")
async def report_link(link_id: str, body: ReportLinkIn, p: Active, svc: Services):
    return ok(await svc.promos.report_link(p.account, link_id, body.details))


@router.get("/promotions/content-ideas")
async def content_ideas(p: Active, svc: Services, region: str | None = Query(default=None, max_length=64)):
    return ok(await svc.promos.content_ideas(p.account, region))


@router.get("/promotions/cards/{match_id}")
async def open_card(match_id: str, p: Active, svc: Services):
    return ok(await svc.promos.card(p.account, match_id))


@router.post("/promotions/listings/{listing_id}/click")
async def open_seller(listing_id: str, p: Active, svc: Services):
    return ok(await svc.promos.click_listing(p.account, listing_id))


@router.get("/promotions/suggested")
async def suggested_sellers(p: Active, svc: Services, category: str = Query(min_length=2, max_length=64),
                            limit: int = Query(default=5, ge=1, le=20)):
    return ok(await svc.promos.suggested(p.account, category, limit))


@router.get("/admin/promotions/keywords")
async def category_keywords(p: Admin, svc: Services):
    grouped: dict[str, list[str]] = {}  # the same shape the PUT takes and returns: category -> keywords
    for keyword, category in (await svc.promos.keywords()).items():
        grouped.setdefault(category, []).append(keyword)
    return ok({category: sorted(words) for category, words in sorted(grouped.items())})


@router.put("/admin/promotions/keywords")
async def set_category_keywords(body: KeywordsIn, p: Admin, svc: Services):
    return ok(await svc.promos.set_keywords(p.account, body.category, body.keywords, body.reason))


# --- admin monitoring: dashboard, every plan, suspend, remove a link. All changes are audit-logged. ------
@router.get("/admin/promotions/overview")
async def promotions_overview(p: Admin, svc: Services, days: int = Query(default=30, ge=1, le=365)):
    return ok(await svc.promos.admin_overview(p.account, days))


@router.get("/admin/promotions")
async def all_plans(p: Admin, svc: Services, status: str | None = None, kind: str | None = None,
                    under_review: bool = False, cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100)):
    return ok_page(await svc.promos.admin_list(status=status, kind=kind, under_review=under_review, cursor=cursor, limit=limit))


@router.get("/admin/promotions/{listing_id}")
async def plan_detail(listing_id: str, p: Admin, svc: Services):
    return ok(await svc.promos.admin_detail(listing_id))


@router.post("/admin/promotions/{listing_id}/suspend")
async def suspend_plan(listing_id: str, body: ReasonIn, p: Admin, svc: Services):
    return ok(await svc.promos.admin_set_suspended(p.account, listing_id, True, body.reason))


@router.post("/admin/promotions/{listing_id}/restore")
async def restore_plan(listing_id: str, body: ReasonIn, p: Admin, svc: Services):
    return ok(await svc.promos.admin_set_suspended(p.account, listing_id, False, body.reason))


@router.delete("/admin/promotions/links/{link_id}")
async def admin_remove_link(link_id: str, body: ReasonIn, p: Admin, svc: Services):
    await svc.promos.admin_remove_link(p.account, link_id, body.reason)
    return ok()
