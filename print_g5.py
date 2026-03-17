import json

with open('database/skeletons/sources/methodology_extracted.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

print(d['grade_5']['full_text'])
