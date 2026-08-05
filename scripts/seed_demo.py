"""Populate the database with demo leads and a two-touch campaign.

Usage: python -m scripts.seed_demo
"""

from __future__ import annotations

from app.db import SessionLocal, init_db
from app.models import Campaign, CampaignStep, Channel, Lead
from app.services import compliance, scheduler

DEMO_LEADS = [
    ("Maria", "Alvarez", "+15551230001", "TX", "term life, $500k"),
    ("James", "Okafor", "+15551230002", "FL", "final expense"),
    ("Priya", "Raman", "+15551230003", "CA", "whole life for kids"),
    ("Tom", "Becker", "+15551230004", "NY", "term life, $250k"),
]


def main() -> None:
    init_db()
    with SessionLocal() as session:
        for first, last, phone, state, interest in DEMO_LEADS:
            if session.query(Lead).filter(Lead.phone == phone).first():
                continue
            lead = Lead(
                first_name=first,
                last_name=last,
                phone=phone,
                state=state,
                timezone=compliance.STATE_TIMEZONES.get(state, "America/New_York"),
                coverage_interest=interest,
                source="demo_seed",
            )
            session.add(lead)
            session.flush()
            for channel in Channel:
                compliance.grant_consent(
                    session, lead, channel, evidence="demo seed: web quote form opt-in"
                )

        campaign = session.query(Campaign).filter(Campaign.name == "New lead cadence").first()
        if campaign is None:
            campaign = Campaign(name="New lead cadence", goal="Book a call with a licensed agent")
            session.add(campaign)
            session.flush()
            session.add_all(
                [
                    CampaignStep(
                        campaign_id=campaign.id,
                        position=1,
                        day_offset=0,
                        channel=Channel.sms,
                        prompt="Introduce yourself and ask if now is a good time.",
                    ),
                    CampaignStep(
                        campaign_id=campaign.id,
                        position=2,
                        day_offset=1,
                        channel=Channel.voice,
                        prompt="Qualify and book a licensed agent callback.",
                    ),
                    CampaignStep(
                        campaign_id=campaign.id,
                        position=3,
                        day_offset=4,
                        channel=Channel.sms,
                        prompt="Last friendly nudge before pausing outreach.",
                    ),
                ]
            )
            session.flush()

        for lead in session.query(Lead).all():
            scheduler.enroll(session, campaign, lead)
        session.commit()
    print("Seeded demo leads and the 'New lead cadence' campaign.")


if __name__ == "__main__":
    main()
