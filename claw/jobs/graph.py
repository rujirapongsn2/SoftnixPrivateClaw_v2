"""Pure DAG validation shared with the existing mission engine."""


class InvalidGraphError(Exception):
    pass


class CycleDetectedError(InvalidGraphError):
    pass


def validate_dag(nodes: list[dict]) -> None:
    ids = [n['id'] for n in nodes]
    if len(set(ids)) != len(ids):
        raise InvalidGraphError('duplicate node ids')
    children = {i: [] for i in ids}
    indegree = {i: 0 for i in ids}
    for node in nodes:
        for dep in node.get('depends_on') or []:
            if dep == node['id']:
                raise CycleDetectedError(f'node {dep!r} depends on itself')
            if dep not in children:
                raise InvalidGraphError(f"node {node['id']!r} depends on unknown node {dep!r}")
            children[dep].append(node['id'])
            indegree[node['id']] += 1
    queue = [i for i in ids if indegree[i] == 0]
    count = 0
    while queue:
        count += 1
        for child in children[queue.pop()]:
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    if count != len(ids):
        raise CycleDetectedError('cycle detected')
