from boss.labels import Route, queue_order, route


def issue(number, *names):
    return {"number": number, "labels": [{"name": n} for n in names]}


def test_defaults_to_implement_p2():
    assert route(set()) == Route("implement", "p2")


def test_reads_kind_and_priority():
    assert route({"do:debug", "priority:p0"}) == Route("debug", "p0")


def test_queue_is_priority_then_oldest():
    queue = [issue(5), issue(3, "priority:p3"), issue(9, "priority:p0"), issue(2)]
    assert [i["number"] for i in queue_order(queue)] == [9, 2, 5, 3]
