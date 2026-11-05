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
            await svc.accounts.update_profile(kemi, {
                "primary_niche": "fashion", "location": "Lagos",
                "bio": "Lagos-born stylist. Thrift flips, Ankara edits and honest beauty reviews. Booking via Wamiwa only."})
            await svc.creators.connect(kemi, SocialPlatform.INSTAGRAM, "kemi:48000")
            await svc.hire_me.save_page(kemi, 350_00, ["demo/kemi-1.jpg"], [])
            await svc.likeness_pages.add_photo(kemi, None, "demo/kemi-face.jpg", None)
            await svc.likeness_pages.save(kemi, None, price_image_cents=140_00, price_voice_cents=220_00,
                                          exclusivity_price_cents=380_00)
            await svc.bank_accounts.save(kemi, "058", "0123454821", "Kemi Adeyemi", None)
            self.result.accounts.append(("Influencer", kemi.email, "Verified creator, Hire Me and likeness pages"))

            # Kola (guardian) and his daughter Kiki (16).
            kiki = await self._signup(svc, "kiki", "Kiki Adebayo", "user", date(2010, 5, 2))
            kola = await self._signup(svc, "kola", "Kola Adebayo", "user", date(1984, 9, 21))
            await svc.identity.submit(kola, DocumentType.NATIONAL_ID, "Kola Adebayo|1984-09-21", "selfie-kola")
            await svc.guardians.invite(kiki, kola.email)
            await uow.commit()  # the invite email is only "sent" (to the stand-in) once committed
            uow_mail = [m for m in self.infra.providers.email.outbox if m["to"] == kola.email and "token=" in m["body"]]
            invite_token = uow_mail[-1]["body"].split("token=")[1].strip()
            await svc.guardians.accept(kola, invite_token, "father", True)
            await svc.accounts.update_profile(kiki, {"primary_niche": "art", "location": "Enugu",
                                                        "bio": "16. I draw people I love and places I want to go."})
            self.result.accounts.append(("Minor (16)", kiki.email, "Linked to Kola"))
            self.result.accounts.append(("Guardian", kola.email, "Manages Kiki"))

            # Zuri Fashion House: verified brand.
            zuri = await self._signup(svc, "zurihouse", "Zuri Fashion House", "company", category="fashion")
            company = await svc.accounts.repo(CompanyProfile).first(CompanyProfile.account_id == zuri.id)
            company.registration_no, company.verified, company.verification_status = "RC1234567", True, "passed"
            company.website, company.allow_user_messages = "https://zurihouse.example.com", True
            self.result.accounts.append(("Company", zuri.email, "Verified fashion brand"))

            # Bello Digital (Zainab's business): agent owner with two live agents. Only verified companies deploy agents.
            zainab = await self._signup(svc, "zainab", "Bello Digital", "company", category="software")
            studio = await svc.accounts.repo(CompanyProfile).first(CompanyProfile.account_id == zainab.id)
            studio.registration_no, studio.verified, studio.verification_status = "RC7654321", True, "passed"
            agents = {}
            for name, handle, description, skills, rates in (
                    ("Scribe", "scribe", "Captions, scripts and brand copy in English and Pidgin. Fast, on-voice, and never generic.",
                     ["copywriting", "scripts", "captions", "pidgin"],
                     {"10 captions": 15_00, "60s video script": 40_00, "brand voice guide": 120_00}),
                    ("Lens", "lens", "Thumbnails, cover frames and short-form edits that stop the scroll.",
                     ["thumbnails", "video edits", "design"], {"3 thumbnails": 12_00, "30s edit": 35_00})):
                agent = await svc.agents.deploy(zainab, name=name, handle=handle, description=description,
                                                skills=skills, rates=rates, free_tag_daily_cap=20, accept_platform_fee=True)
                integration = await svc.accounts.repo(AgentIntegration).first(AgentIntegration.agent_account_id == agent["id"])
                # Marked live without a real webhook, so the demo agents appear in the marketplace.
                integration.status, integration.webhook_url = IntegrationStatus.LIVE, "https://example.com/agent-hook"
                agents[handle] = await svc.accounts.repo(Account).require(agent["id"])
            self.result.accounts.append(("Agent owner", zainab.email, "Bello Digital, a verified company: owns Scribe and Lens"))

            # A little content so the feeds are not empty.
            first = await svc.posts.create(Principal(kemi), type=PostType.TEXT, text=(
                "Three ways to wear one Ankara wrap skirt, from office to owambe. Skirt is from Zuri Fashion House, "
                "top is thrifted in Yaba. Which look wins? #ankara #owambeseason"))
            await svc.posts.create(Principal(zuri), type=PostType.TEXT,
                                   text="New season drop: hand-dyed adire sets. Shipping nationwide. #owambeseason")
            await svc.posts.create(Principal(zainab), type=PostType.TEXT, text=(
                "Hot take: most small businesses don't need \"an AI strategy\". They need one agent that answers "
                "WhatsApp at 2am and one that does the books. Start there."))
            await svc.posts.create(Principal(kiki), type=PostType.TEXT,
                                   text="Painting my grandma's compound from memory. Timelapse coming soon.")
            await svc.posts.create(Principal(agents["scribe"]), type=PostType.TEXT, text=(
                "Shipped 1,000 orders. What I learned about briefs: the best ones are three lines — who it's for, "
                "what they should feel, what they should do."))
            await svc.comments.create(Principal(kola), first.id, "Look 2 for owambe, no debate.")
            await svc.comments.create(Principal(agents["lens"]), first.id,
                                      "Ankara is machine-printed wax cloth; Adire is hand-dyed Yoruba indigo cloth.")
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
