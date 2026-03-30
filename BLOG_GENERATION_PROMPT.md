# PostgreSQL Weekly Blog Generation Prompt Template

## Basic Prompt Template

Use this template for each thread you want to convert into a blog post. Simply replace `{THREAD_ID_OR_URL}` with your actual thread ID or URL.

---

### Prompt:

```
I need you to act as a PostgreSQL expert and technical writer to generate a high-quality blog post from a PostgreSQL mailing list thread.

**Thread ID/URL:** {THREAD_ID_OR_URL}

**Instructions:**

1. **Fetch the thread data:**
   - Run: `python3 tools/fetch_data.py --thread-id "{THREAD_ID_OR_URL}"`
   - This will download the HTML, convert to Markdown, and save attachments

2. **Verify all patch set versions are downloaded:**
   - Identify all patch versions referenced in the thread (v1, v2, v3…; or 0001-, 0002- in patch series)
   - Ensure every referenced version exists in `attachments/`
   - If any are missing, run `python3 tools/fetch_data.py --thread-dir "data/threads/YYYY-MM-DD/<thread-id>"` to retry; do not proceed until all are present

3. **Analyze the content:**
   - Read the converted Markdown file in `data/threads/YYYY-MM-DD/<thread-id>/thread.md`
   - Review the original HTML if you need more context
   - Check the `attachments/` folder for any patch files
   - If there are multiple patch versions (v1, v2, v3, etc.), use `diff` to understand what changed between versions

4. **Generate a technical blog post with:**
   - **Clear title:** Based on the main topic discussed
   - **Introduction:** Brief context and why this matters
   - **Technical Analysis:**
     - Key discussion points and decisions
     - Code examples or patch highlights (if relevant)
     - **SQL examples:** When the topic can be shown with SQL (functions, DDL, queries, `EXPLAIN`, patterns from regression tests), add a subsection with copy-pasteable `sql` blocks in **both** English and Chinese; include version/caveats if needed. Skip only when SQL would be misleading or not applicable—see `SQL examples convention` in `.cursor/skills/pgweekly-blog-generation/SKILL.md`.
     - Evolution of the solution (compare patch versions if multiple exist)
   - **Community Insights:**
     - Important reviewer feedback
     - Issues discovered and how they were resolved
   - **Technical Details:**
     - Implementation approaches
     - Edge cases discussed
     - Performance considerations (if any)
   - **Current Status:** Where the patch/discussion stands
   - **Conclusion:** Summary and implications for PostgreSQL users

5. **Write TWO versions (English and Chinese):**
   - **English version:** Professional technical writing style, clear explanations
   - **Chinese version:** Professional Chinese technical writing, natural terminology
   - **Both versions:** Code blocks with proper syntax highlighting, links to documentation

6. **Save the blogs:**
   - Determine the appropriate year and week number based on the thread date or current date
   - Generate a descriptive filename based on the content
   - **Create directories if needed and save TWO files:**
     - `src/en/{year}/{week}/{filename}.md` - English version
     - `src/cn/{year}/{week}/{filename}.md` - Chinese version
   - Update `src/SUMMARY.md` to include BOTH blog entries in their respective language sections
   - Update both language week README files if they don't exist

**Additional Context:**
- You have full access to all downloaded files in the thread directory
- Use your PostgreSQL expertise to provide valuable insights
- Focus on technical accuracy and clarity
- Highlight what developers and DBAs should know about this discussion
- **Year/Week determination:** Check the thread's date in metadata.txt or use the current date to determine the appropriate year and ISO week number for organizing the blog

Please start by fetching the data and then generate the blog post.
```

---

## Advanced Prompt Template (With Specific Requirements)

Use this version when you want more control over the output:

```
Act as a PostgreSQL core developer and technical writer. Generate a comprehensive blog post from the following mailing list thread.

**Thread:** {THREAD_ID_OR_URL}

**Step 1: Data Collection**
Run: `python3 tools/fetch_data.py --thread-id "{THREAD_ID_OR_URL}"`

**Step 2: Verify Patch Versions**
- Identify all patch versions referenced in the thread (v1, v2, v3…; 0001-, 0002-…)
- Ensure every referenced version exists in `attachments/`; if any missing, run `python3 tools/fetch_data.py --thread-dir "data/threads/YYYY-MM-DD/<thread-id>"` and do not proceed until all are present

**Step 3: Content Analysis**
Review:
- Markdown content: `data/threads/*/thread.md`
- Patches in: `data/threads/*/attachments/`
- For multiple patch versions, run: `diff -u v1-*.patch v2-*.patch` to see evolution

**Step 4: Blog Structure**

Create a blog with these sections:

### Title
[Generate a concise, descriptive title]

### Metadata
- Date: [Today's date]
- Category: PostgreSQL Development
- Tags: [Extract relevant tags from content]

### Introduction (2-3 paragraphs)
- What problem does this address?
- Why is this discussion important?
- Who should care about this?

### Background
- Current limitations or issues
- Motivation for the proposed change

### Technical Deep Dive
- **Proposed Solution:** Explain the approach
- **Implementation Details:** Key technical points
- **Code Walkthrough:** Highlight important code sections from patches
- **Evolution:** If multiple patch versions exist, show how the solution evolved
  - Compare patches using diff
  - Explain what changed and why

### Community Discussion
- Key reviewer comments
- Issues/bugs discovered
- Suggested improvements
- Debates or alternative approaches

### Technical Considerations
- Performance implications
- Backward compatibility
- Security considerations
- Edge cases and how they're handled

### Patch Evolution (if applicable)
For each major version:
- v1: [Initial approach]
- v2: [What changed - use actual diff output]
- v3: [Further refinements]

### Current Status
- Review status
- Remaining concerns
- Expected timeline (if mentioned)

### Impact & Use Cases
- Who will benefit?
- Example use cases
- Migration considerations

### Conclusion
- Summary of key points
- Broader implications
- What to watch for

### References
- Link to mailing list thread
- Related documentation
- Previous related discussions (if mentioned)

**Step 5: File Management**
- Determine year and week: [Calculate from thread date or today's date]
- Create paths: `src/en/{year}/{week}/` and `src/cn/{year}/{week}/`
- Filename: [Generate from main topic, lowercase-with-hyphens]
- Save English version to `src/en/{year}/{week}/{filename}.md`
- Save Chinese version to `src/cn/{year}/{week}/{filename}.md`
- Update `src/SUMMARY.md` in both language sections

**Writing Guidelines:**
- Use clear, technical language
- Include code blocks with syntax highlighting
- Add inline comments for complex code
- Use bullet points for clarity
- Bold important terms on first use
- Length: 1500-2500 words
- Tone: Professional, educational, engaging

**Quality Checks:**
- [ ] All technical terms correctly explained
- [ ] Code examples are accurate
- [ ] Patch comparisons are clear
- [ ] Links are functional
- [ ] SUMMARY.md is updated
- [ ] File is in correct directory

Begin the analysis and blog generation now.
```

---

## Quick Prompt (Minimal Version)

For faster processing:

```
Generate a PostgreSQL technical blog from this thread: {THREAD_ID_OR_URL}

1. Fetch: `python3 tools/fetch_data.py --thread-id "{THREAD_ID_OR_URL}"`
2. Verify all patch set versions referenced in the thread are downloaded; retry with `--thread-dir` if any are missing; do not proceed until all are present
3. Read the Markdown content and patches
4. Compare patch versions if multiple exist (use diff)
5. Write a technical blog covering:
   - What problem is being solved
   - How the solution works
   - Key discussion points
   - Patch evolution (if applicable)
   - Current status
6. Save to: `src/{year}/{appropriate-week}/{descriptive-name}.md`
7. Update `src/SUMMARY.md`

Write as a PostgreSQL expert. Focus on technical accuracy and clarity.
```

---

## Example Usage

### Example 1: Simple usage
```
I need you to act as a PostgreSQL expert and technical writer to generate a high-quality blog post from a PostgreSQL mailing list thread.

**Thread ID/URL:** https://www.postgresql.org/message-id/flat/CACJufxGn+bMNPyrMTe0-W4fLmkFVXSr-6cvFos9mGsp-5u-RXw@mail.gmail.com

[... rest of basic prompt ...]
```

### Example 2: Just the thread ID
```
Generate a PostgreSQL technical blog from this thread: CACJufxGn+bMNPyrMTe0-W4fLmkFVXSr-6cvFos9mGsp-5u-RXw@mail.gmail.com

[... rest of quick prompt ...]
```

---

## Tips for Best Results

1. **Let the Agent Decide:** Don't micromanage the filename or exact structure
2. **Provide Context:** If you know the thread is about a specific feature, mention it
3. **Review Output:** Always review the generated blog for technical accuracy
4. **Iterate:** Ask for improvements: "Make the technical section more detailed" or "Add more code examples"

---

## Multi-Thread Batch Processing Prompt

For processing multiple threads at once:

```
I have {N} PostgreSQL mailing list threads to convert into blog posts. Process them one by one:

**Threads:**
1. {THREAD_ID_1}
2. {THREAD_ID_2}
3. {THREAD_ID_3}

For each thread:
1. Fetch data: `python3 tools/fetch_data.py --thread-id "{THREAD_ID}"`
2. Verify all patch set versions referenced in the thread are downloaded; retry with `--thread-dir` if needed; do not proceed until all are present
3. Generate TWO versions of the technical blog post (English and Chinese)
4. Save to appropriate year/week directories:
   - English: `src/en/{year}/{week}/{filename}.md`
   - Chinese: `src/cn/{year}/{week}/{filename}.md`
5. Update `src/SUMMARY.md` in both language sections

After all are done, provide a summary of:
- Blogs created
- File locations
- Any issues encountered

Use the same PostgreSQL expert approach for all blogs.
```

---

## Customization Variables

You can customize these aspects in your prompt:

- `{THREAD_ID_OR_URL}` - Required: The thread to process
- `{SPECIFIC_FOCUS}` - Optional: "Focus on performance implications"
- `{TARGET_AUDIENCE}` - Optional: "Write for DBAs" or "Write for developers"
- `{BLOG_LENGTH}` - Optional: "Keep it under 1000 words" or "Comprehensive analysis"
- `{TONE}` - Optional: "Conversational" or "Academic"
- `{WEEK_NUMBER}` - Optional: Specify the week if you know it

---

## Common Follow-up Prompts

After initial generation:

- "Make the technical section more detailed with code examples"
- "Add a comparison table for the different patch versions"
- "Simplify the explanation for junior developers"
- "Add more context about why this feature is important"
- "Compare this approach with how other databases solve this"
- "Add a 'Try it yourself' section with examples"
