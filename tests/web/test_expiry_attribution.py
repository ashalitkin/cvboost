from __future__ import annotations

from tests.web.conftest import Ids


def test_closed_groups_leave_inbox_and_410(client, seeded: Ids):
    text = client.get("/inbox").text
    assert "Expired Himalayas" not in text and "Expired WWR" not in text
    for cid in (seeded.himalayas_dead, seeded.wwr_dead):
        r = client.get(f"/jobs/{cid}")
        assert r.status_code == 410
        assert "plain text body" not in r.text and "https://" not in r.text.split("<main>")[1]


def test_mixed_group_hides_only_closed_member(client, seeded: Ids):
    text = client.get("/inbox").text
    assert "Mixed Group Role" in text
    assert "himalayas.app/jobs/dead-link" not in text and "via Himalayas" not in text
    job = client.get(f"/jobs/{seeded.mixed_alive}")
    assert job.status_code == 200 and "dead-link" not in job.text
    assert "via Remotive" in job.text


def test_attribution_links_follow_and_nofollow_only_in_descriptions(client, seeded: Ids):
    inbox = client.get("/inbox").text
    assert (
        'href="https://remoteok.com/remote-jobs/1" rel="noopener noreferrer" '
        'target="_blank">via Remote OK' in inbox
    )
    assert "via Remotive" in inbox
    assert "nofollow" not in inbox.split("<main>")[1]
    job = client.get(f"/jobs/{seeded.xss}").text
    assert 'rel="noopener noreferrer nofollow"' not in job.split("Description")[0]
