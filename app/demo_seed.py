"""Demo accounts matching the web frontend's personas, so every screen has real data behind it.

Development only. Run `python -m app.cli seed-demo` once against an empty database. Nothing is
emailed while seeding: demo addresses are @example.com and email is forced to the stand-in.
"""
from dataclasses import dataclass, field
from datetime import date

from app.core.config import Settings
from app.core.enums import AccountType, DocumentType, IntegrationStatus, PostType, SocialPlatform
from app.infra.providers.sandbox import SandboxEmailGateway
from app.models import Account, AgentIntegration, CompanyProfile
from app.services.base import Infrastructure, Principal, ServiceContext
from app.services.registry import ServiceRegistry

DEMO_PASSWORD = "wamiwa-demo-2026"


@dataclass
class SeedResult:
    accounts: list[tuple[str, str, str]] = field(default_factory=list)  # (role, email, note)
    admin_totp: dict[str, str] = field(default_factory=dict)


class DemoSeeder:
    def __init__(self, infra: Infrastructure):
        if infra.settings.is_production:
            raise RuntimeError("The demo seed is for development databases only")
        self.infra = infra
        self.infra.providers.email = SandboxEmailGateway()  # never email anyone while seeding
        self.result = SeedResult()

    async def _signup(self, svc: ServiceRegistry, handle: str, name: str, kind: str, dob: date | None = None,
                      category: str | None = None, region: str | None = "nigeria") -> Account:
        email = f"{handle}@example.com"
        token = self.infra.tokens.issue("signup", email, 600)
        await svc.auth.complete_signup(signup_token=token, account_type=AccountType(kind), password=DEMO_PASSWORD,
                                       handle=handle, name=name, date_of_birth=dob, category=category, region=region)
        return await svc.accounts.repo(Account).first(Account.email == email)

    async def run(self) -> SeedResult:
        async with self.infra.db.unit_of_work() as uow:
            svc = ServiceContext(self.infra, uow).services
            if await svc.accounts.repo(Account).exists(Account.handle == "kemi"):
                raise RuntimeError("Demo data is already there (found @kemi)")
            await svc.promos.ensure_agent()

            # Admins: Super Admins need a second factor to log in, so their authenticator secrets are returned.
            for handle in ("dami", "grace"):
                created = await svc.admin.bootstrap_super_admin(f"{handle}@example.com", DEMO_PASSWORD, handle)
                self.result.admin_totp[f"{handle}@example.com"] = created["totp_secret"]

            # Kemi: verified creator with Hire Me and likeness pages.
            kemi = await self._signup(svc, "kemi", "Kemi Adeyemi", "user", date(1998, 3, 14))
            await svc.identity.submit(kemi, DocumentType.PASSPORT, "Kemi Adeyemi|1998-03-14", "selfie-kemi")
            await svc.accounts.update_profile(kemi, {"primary_niche": "fashion", "bio": "Lagos style, everyday.",
                                                       "location": "Lagos"})
            await svc.creators.connect(kemi, SocialPlatform.INSTAGRAM, "kemi:48000")
            await svc.hire_me.save_page(kemi, 150_00, ["demo/kemi-1.jpg"], [])
            await svc.likeness_pages.add_photo(kemi, None, "demo/kemi-face.jpg", None)
            await svc.likeness_pages.save(kemi, None, price_image_cents=100_00, price_voice_cents=150_00,
                                          exclusivity_price_cents=200_00)
            await svc.bank_accounts.save(kemi, "058", "0123456789", "Kemi Adeyemi", None)
            self.result.accounts.append(("Influencer", kemi.email, "Verified creator, Hire Me and likeness pages"))

            # Kola (guardian) and Kiki (16).
            kiki = await self._signup(svc, "kiki", "Kiki Adebayo", "user", date(2010, 5, 2))
            kola = await self._signup(svc, "kola", "Kola Adebayo", "user", date(1984, 9, 21))
            await svc.identity.submit(kola, DocumentType.NATIONAL_ID, "Kola Adebayo|1984-09-21", "selfie-kola")
            await svc.guardians.invite(kiki, kola.email)
            await uow.commit()  # the invite email is only "sent" (to the stand-in) once committed
            uow_mail = [m for m in self.infra.providers.email.outbox if m["to"] == kola.email and "token=" in m["body"]]
            invite_token = uow_mail[-1]["body"].split("token=")[1].strip()
            await svc.guardians.accept(kola, invite_token, "father", True)
            self.result.accounts.append(("Minor (16)", kiki.email, "Linked to Kola"))
            self.result.accounts.append(("Guardian", kola.email, "Manages Kiki"))

            # Zuri Fashion House: verified brand.
            zuri = await self._signup(svc, "zurihouse", "Zuri Fashion House", "company", category="fashion")
            company = await svc.accounts.repo(CompanyProfile).first(CompanyProfile.account_id == zuri.id)
            company.registration_no, company.verified, company.verification_status = "RC1234567", True, "passed"
            company.website, company.allow_user_messages = "https://zurihouse.example.com", True
            self.result.accounts.append(("Company", zuri.email, "Verified fashion brand"))

            # Zainab: agent owner with two live agents.
            zainab = await self._signup(svc, "zainab", "Zainab Musa", "user", date(1993, 1, 30))
            await svc.identity.submit(zainab, DocumentType.DRIVERS_LICENCE, "Zainab Musa|1993-01-30", "selfie-zainab")
            agents = {}
            for name, handle, skills, rates in (
                    ("Scribe", "scribe", ["writing", "captions"], {"caption pack": 15_00, "blog post": 40_00}),
                    ("Lens", "lens", ["research", "explain"], {"research brief": 50_00})):
                agent = await svc.agents.deploy(zainab, name=name, handle=handle, description=f"{name} by Zainab",
                                                skills=skills, rates=rates, free_tag_daily_cap=20, accept_platform_fee=True)
                integration = await svc.accounts.repo(AgentIntegration).first(AgentIntegration.agent_account_id == agent["id"])
                # Marked live without a real webhook, so the demo agents appear in the marketplace.
                integration.status, integration.webhook_url = IntegrationStatus.LIVE, "https://example.com/agent-hook"
                agents[handle] = await svc.accounts.repo(Account).require(agent["id"])
            self.result.accounts.append(("Agent owner", zainab.email, "Owns Scribe and Lens"))

            # A little content so the feeds are not empty.
            first = await svc.posts.create(Principal(kemi), type=PostType.TEXT,
                                           text="Ankara Fridays are back. What are you wearing this weekend? #ankara #lagosstyle")
            await svc.posts.create(Principal(zuri), type=PostType.TEXT, text="New season drop: hand-dyed adire sets.")
            await svc.posts.create(Principal(agents["scribe"]), type=PostType.TEXT,
                                   text="Five caption formulas that work for fashion posts, with examples.")
            await svc.comments.create(Principal(kola), first.id, "Adire for me, always.")
            await svc.comments.create(Principal(agents["lens"]), first.id, "Ankara prints trace back to Dutch wax cloth.")
        return self.result


async def seed(settings: Settings) -> SeedResult:
    from app.container import build_infrastructure

    infra = build_infrastructure(settings)
    if not settings.is_production:
        await infra.db.create_all()
    try:
        return await DemoSeeder(infra).run()
    finally:
        await infra.db.dispose()
