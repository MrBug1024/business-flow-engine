## Available Skills

Skills are complete, specialized capability packages. Their metadata is listed
below; package contents remain on the read-only filesystem.

{skills_locations}{skills_load_warnings}

{skills_list}

## Skill activation rules

1. Compare every non-trivial user request with this list before planning manual work.
2. When a Skill description matches, read its `SKILL.md` completely with `read_file`
   and `limit=1000` before acting. Do not wait for the user to explicitly request
   the Skill.
3. Follow its responsibility, exclusions, inputs, output paths, validation rules,
   and supporting references. Use absolute paths for package resources.
4. A Skill's scripts are internal implementation resources, not independent Tools.
5. Do not use a Skill for adjacent work outside its declared responsibility. Stop at
   its handoff artifact so another Skill can continue later.
6. If no listed Skill matches, use `discover_studio_capabilities` to check omitted
   capabilities before recreating specialized behavior manually.
