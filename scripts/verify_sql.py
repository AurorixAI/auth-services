"""Verify SQL skeleton integrity: parent_id refs, duplicate IDs, prerequisite cycles."""
import re, sys, os, collections

def parse_inserts(sql_text: str):
    """Extract all INSERT value rows from SQL text."""
    rows = []
    for m in re.finditer(r"\('([^']+)',\s*'(L[1-4])',\s*(?:NULL|'([^']*?)'),", sql_text):
        rows.append({"id": m.group(1), "level": m.group(2), "parent_id": m.group(3)})
    return rows

def parse_prereqs(sql_text: str):
    """Extract prerequisite edges."""
    edges = []
    for m in re.finditer(r"\('(G\d+_S\d+_\d+)',\s*'(G\d+_S\d+_\d+)',\s*'(hard|soft)'", sql_text):
        edges.append({"skill_id": m.group(1), "prerequisite_id": m.group(2), "type": m.group(3)})
    return edges

def parse_tasks(sql_text: str):
    """Extract task skill references."""
    tasks = []
    for m in re.finditer(r"\('(G\d+_TASK_\d+)',\s*'(G\d+_S\d+_\d+)'", sql_text):
        tasks.append({"task_id": m.group(1), "skill_id": m.group(2)})
    return tasks

def detect_cycles(edges):
    """Check for cycles in prerequisite graph using DFS."""
    graph = collections.defaultdict(list)
    for e in edges:
        graph[e["skill_id"]].append(e["prerequisite_id"])
    
    visited = set()
    rec_stack = set()
    
    def dfs(node, path):
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                cycle = dfs(neighbor, path + [neighbor])
                if cycle:
                    return cycle
            elif neighbor in rec_stack:
                return path + [neighbor]
        rec_stack.discard(node)
        return None
    
    all_nodes = set(graph.keys())
    for e in edges:
        all_nodes.add(e["prerequisite_id"])
    
    for node in all_nodes:
        if node not in visited:
            cycle = dfs(node, [node])
            if cycle:
                return cycle
    return None

def verify_file(filepath: str):
    """Run all integrity checks on a SQL file."""
    filename = os.path.basename(filepath)
    print(f"\n{'='*60}")
    print(f"  {filename}")
    print(f"{'='*60}")
    
    with open(filepath, "r", encoding="utf-8") as f:
        sql = f.read()
    
    rows = parse_inserts(sql)
    prereqs = parse_prereqs(sql)
    tasks = parse_tasks(sql)
    
    # Counts by level
    counts = collections.Counter(r["level"] for r in rows)
    print(f"\nCounts: L1={counts.get('L1',0)}, L2={counts.get('L2',0)}, "
          f"L3={counts.get('L3',0)}, L4={counts.get('L4',0)}")
    print(f"Prerequisites: {len(prereqs)}")
    print(f"Tasks: {len(tasks)}")
    
    all_ids = {r["id"] for r in rows}
    errors = 0
    
    # 1. Check for duplicate IDs
    id_counts = collections.Counter(r["id"] for r in rows)
    dupes = {k: v for k, v in id_counts.items() if v > 1}
    if dupes:
        print(f"\n❌ DUPLICATE IDs: {dupes}")
        errors += len(dupes)
    else:
        print("\n✅ No duplicate IDs")
    
    # 2. Check parent_id references
    bad_parents = []
    for r in rows:
        if r["parent_id"] and r["parent_id"] not in all_ids:
            bad_parents.append(f"  {r['id']} -> parent {r['parent_id']} NOT FOUND")
    if bad_parents:
        print(f"\n❌ Broken parent_id refs ({len(bad_parents)}):")
        for bp in bad_parents:
            print(bp)
        errors += len(bad_parents)
    else:
        print("✅ All parent_id references valid")
    
    # 3. Check prerequisite references
    l4_ids = {r["id"] for r in rows if r["level"] == "L4"}
    bad_prereqs = []
    for p in prereqs:
        if p["skill_id"] not in l4_ids:
            bad_prereqs.append(f"  skill_id {p['skill_id']} NOT in L4")
        if p["prerequisite_id"] not in l4_ids:
            bad_prereqs.append(f"  prerequisite_id {p['prerequisite_id']} NOT in L4")
    if bad_prereqs:
        print(f"\n❌ Broken prerequisite refs ({len(bad_prereqs)}):")
        for bp in bad_prereqs:
            print(bp)
        errors += len(bad_prereqs)
    else:
        print("✅ All prerequisite references valid")
    
    # 4. Check for cycles
    cycle = detect_cycles(prereqs)
    if cycle:
        print(f"\n❌ CYCLE DETECTED: {' -> '.join(cycle)}")
        errors += 1
    else:
        print("✅ No cycles in prerequisite graph")
    
    # 5. Check task skill references
    bad_tasks = []
    for t in tasks:
        if t["skill_id"] not in l4_ids:
            bad_tasks.append(f"  {t['task_id']} -> skill {t['skill_id']} NOT in L4")
    if bad_tasks:
        print(f"\n❌ Broken task refs ({len(bad_tasks)}):")
        for bt in bad_tasks:
            print(bt)
        errors += len(bad_tasks)
    else:
        print("✅ All task skill references valid")
    
    # 6. hierarchy check: every L2 has L1 parent, every L3 has L2 parent, every L4 has L3 parent
    level_ids = {lvl: {r["id"] for r in rows if r["level"] == lvl} for lvl in ["L1","L2","L3","L4"]}
    parent_map = {r["id"]: r["parent_id"] for r in rows}
    
    bad_level_parents = []
    for r in rows:
        expected_parent_level = {"L2": "L1", "L3": "L2", "L4": "L3"}.get(r["level"])
        if expected_parent_level and r["parent_id"]:
            if r["parent_id"] not in level_ids[expected_parent_level]:
                bad_level_parents.append(
                    f"  {r['id']} ({r['level']}) -> parent {r['parent_id']} not in {expected_parent_level}")
    if bad_level_parents:
        print(f"\n❌ Wrong parent level ({len(bad_level_parents)}):")
        for bp in bad_level_parents:
            print(bp)
        errors += len(bad_level_parents)
    else:
        print("✅ Hierarchy levels correct (L2→L1, L3→L2, L4→L3)")
    
    # 7. Check duplicate prerequisite edges
    edge_set = set()
    dup_edges = []
    for p in prereqs:
        key = (p["skill_id"], p["prerequisite_id"])
        if key in edge_set:
            dup_edges.append(key)
        edge_set.add(key)
    if dup_edges:
        print(f"\n❌ Duplicate prerequisite edges ({len(dup_edges)}):")
        for de in dup_edges:
            print(f"  {de[0]} -> {de[1]}")
        errors += len(dup_edges)
    else:
        print("✅ No duplicate prerequisite edges")
    
    print(f"\n{'✅ ALL CHECKS PASSED' if errors == 0 else f'❌ {errors} ERROR(S) FOUND'}")
    return errors

if __name__ == "__main__":
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skeletons = os.path.join(base, "database", "skeletons")
    
    total = 0
    for fname in sorted(os.listdir(skeletons)):
        if fname.startswith("grade_0") and fname.endswith(".sql"):
            total += verify_file(os.path.join(skeletons, fname))
    
    print(f"\n{'='*60}")
    print(f"  TOTAL: {'ALL OK' if total == 0 else f'{total} errors'}")
    print(f"{'='*60}")
    sys.exit(0 if total == 0 else 1)
