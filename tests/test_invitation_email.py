"""Every invitation email carries the code itself and the link that opens it."""
from app.services import invite_service

CODE = "AB1234CD"
LINK = f"https://www.tripagent.vip/claim?code={CODE}"


def test_referral_email_shows_the_code_and_the_link():
    html = invite_service._referral_invitation_email_html(
        friend_name="Asha", inviter_name="Ravi Kumar", invitation_url=LINK, invite_code=CODE
    )
    assert CODE in html
    assert LINK in html
    assert "{{" not in html  # no merge field left unfilled


def test_approval_email_shows_the_code_and_the_link():
    html = invite_service._invitation_approved_request_email_html(invite_code=CODE, expires_on="6 November 2026", link=LINK)
    assert CODE in html
    assert LINK in html
