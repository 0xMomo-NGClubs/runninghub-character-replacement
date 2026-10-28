# H3 reference prompt contract

A concise independently written summary. Check the current provider contract if formats change.

Six English sections in order: subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music. Dialogue, lyrics and actual scene text retain their language.

- Subject labels define reusable visible identities, environment and props.
- Picture labels follow actual image order. A concrete first/key/last frame has its own definition; a character-only image can be cited inside its Subject definition.
- Video labels refer to source edits or temporal/action references in actual order.
- Audio labels exist only for a defined audio relationship, not merely because a file contains sound.

For editing begin the summary with `[video editing + reference generation] The target video is an edited version of <Video 1>.` Add keyframe completion when a concrete frame anchors a shot.

Visual retention markers: fully_preserved, partially_preserved, attribute_transfer, weak_reference. They state desired relationships, not a claim of passed QA. Audio markers: fully_copy, partially_copy, reference, weak_reference.

Opening is [Shot1] written with a space as `[Shot 1]`, without an At timestamp. A real later cut uses `[Shot 2] At MM:SS.mmm, ...`. Do not invent cuts just to describe phases of a continuous source shot.

Describe each segment's observable starting posture, hand/foot/prop actions, rhythm and ending separately. An idle tail needs an idle description; generic athletic prose can rewrite a casual performance.

Speech uses a stable speaker id outside the tag: `<Subject 1> (S1) says: <d>[Chinese] 你好。</d>`. Keep names and delivery directions outside the dialogue tag.

If depth is absent remove its definition and every Video2 reference. Multi-person work requires explicit replacement mapping; this single-person helper does not automatically pick the intended person in a crowd.

Sources:
- https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md
- https://www.minimax.io/news/minimax-h3-open-source
