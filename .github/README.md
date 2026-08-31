# GitHub automation

## Optional project assignment

The `Add to project` workflow adds opened, reopened, or labeled issues and pull
requests to a GitHub Project. Enable it under repository **Settings → Secrets and
variables → Actions** by configuring both:

- Variable `ADD_TO_PROJECT_URL`: the existing project's full URL, such as
  `https://github.com/users/OWNER/projects/NUMBER` or
  `https://github.com/orgs/ORG/projects/NUMBER`. Copy it from the intended project;
  project number `1` is not assumed.
- Secret `ADD_TO_PROJECT_PAT`: a personal access token with write access to that
  project and read access to the repository's issues and pull requests. Follow
  the [action's token setup instructions](https://github.com/actions/add-to-project#inputs)
  for the chosen token type and owner.

If either setting is missing, the workflow reports a notice and skips assignment.
This also covers pull requests from forks where GitHub withholds secrets. It does
not fall back to `GITHUB_TOKEN`, whose repository permissions do not grant access
to user or organization Projects. No project is created by this workflow.

If both settings are present, API failures still fail the workflow. For
`Could not resolve to a ProjectV2 with the number ...`, verify the URL's owner and
number, that the project still exists, and that the token can access it. This
message alone cannot distinguish an absent project from insufficient access.

The action is pinned to the v2.0.0 commit, which uses Node 24. The Node 20
deprecation warning was separate from the project lookup error; downgrading the
runner's Node runtime would not fix project access.
