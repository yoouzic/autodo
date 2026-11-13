"""Explainable policy: priority orders work, complexity/capabilities choose workers."""
from collections.abc import Mapping

from .core import task_tags

EXECUTORS = ('codex', 'dsh', 'zcode', 'qoder', 'antigravity', 'workbuddy', 'codebuddy', 'doubao')
DEFAULT_ROUTES = {
    'easy': ['dsh', 'codex'],
    'medium': ['zcode', 'qoder', 'codex'],
    'hard': ['codex', 'antigravity'],
    'code:easy': ['qoder', 'codex'],
    'code:medium': ['qoder', 'codex'],
    'document:hard': ['codex', 'antigravity'],
}


class RoutingError(ValueError):
    pass


def task_available_executors(row, config, registry):
    """Apply task capabilities consistently before previewing or executing a route."""
    available = {name: bool(spec.get('enabled')) if isinstance(spec, Mapping) else bool(spec)
                 for name, spec in registry.items()}
    from .result_protocol import resolve_delivery, ANSWER_EXECUTORS
    if row.get('payload') and resolve_delivery(row, 'codex')['mode'] == 'answer':
        return {name: enabled and name in ANSWER_EXECUTORS for name, enabled in available.items()}
    if row['kind'] == 'code':
        repositories = config.get('repositories') or {}
        checks = config.get('repository_checks') or {}
        checked_repos = {str(repositories[alias]) for alias in checks if alias in repositories}
        if not row.get('repo') or str(row['repo']) not in checked_repos:
            available['qoder'] = False
            available['workbuddy'] = False
            available['codebuddy'] = False
            available['doubao'] = False
    return available


def choose_executor(task, difficulty, kind, config, available, manual_executor=None):
    if manual_executor is not None:
        if manual_executor not in EXECUTORS:
            raise RoutingError(f'Unknown manual executor: {manual_executor}')
        if not available.get(manual_executor):
            raise RoutingError(f'Manual executor {manual_executor} is unavailable or disabled; inspect agents')
        return {'executor': manual_executor, 'automatic': False, 'fallback': False,
                'reason': f'Manual assignment: {manual_executor}', 'requested': manual_executor}
    explicit = {tag.split(':', 1)[1].strip().lower() for tag in task_tags(task)
                if tag.lower().startswith('agent:')}
    if len(explicit) > 1:
        raise RoutingError('Multiple agent: tags conflict; choose one executor')
    if explicit:
        name = next(iter(explicit))
        if name not in EXECUTORS:
            raise RoutingError(f'Unknown executor: {name}')
        if not available.get(name):
            raise RoutingError(f'Explicit executor {name} is unavailable or disabled; inspect agents')
        return {'executor':name, 'automatic':False, 'fallback':False,
                'reason':f'Explicit agent:{name} tag', 'requested':name}
    routes = dict(DEFAULT_ROUTES)
    routes.update(config.get('executor_routes', {}))
    # A user-provided generic route overrides the stock kind-specific default.
    custom = config.get('executor_routes', {})
    route_key = f'{kind}:{difficulty}'
    if route_key in custom:
        route = custom[route_key]
    elif difficulty in custom:
        route = custom[difficulty]
    else:
        route = routes.get(route_key, routes.get(difficulty, ['codex']))
    if not isinstance(route, list) or not route or any(n not in EXECUTORS for n in route):
        raise RoutingError('Executor routes must be nonempty arrays of supported names')
    for position, name in enumerate(route):
        if available.get(name):
            reason = f'{kind}/{difficulty} route: ' + ' → '.join(route)
            if position:
                reason += '; unavailable before launch: ' + ', '.join(route[:position])
            return {'executor':name, 'automatic':True, 'fallback':position>0,
                    'reason':reason, 'requested':route[0]}
    raise RoutingError('No available executor for route: ' + ', '.join(route))
