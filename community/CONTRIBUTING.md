# Contributing to ARG

ARG is an open source project and contributions are welcome from engineers, security teams, compliance practitioners, and AI governance researchers.

## What We Are Looking For

- New schema definitions for emerging governance use cases
- Configuration templates for additional streaming platforms (AWS Kinesis, GCP Pub/Sub)
- Reference implementations in the examples directory
- Documentation improvements and corrections
- Real-world deployment patterns and lessons learned

## How to Contribute

### Reporting Issues
Open an issue on GitHub. Include the relevant schema or config file, the problem you encountered, and your deployment context where possible.

### Submitting Changes

1. Fork the repository
2. Create a feature branch from `main`: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Ensure schema files are valid JSON Schema (draft-07)
5. Ensure YAML files are valid and well-commented
6. Submit a pull request with a clear description of the change and the problem it solves

### Pull Request Standards

- One logical change per pull request
- Schema changes must include a description of the governance use case they address
- Breaking changes to existing schemas require a version bump and migration notes
- All contributions must be compatible with the MIT license

## Schema Versioning

ARG schemas follow semantic versioning. Breaking changes increment the major version. Additive changes increment the minor version.

## Code of Conduct

See `CODE_OF_CONDUCT.md`. Be direct, be specific, and be respectful of other contributors time.

## Questions

Open a GitHub Discussion for architecture questions or governance use cases you are trying to solve. Issues are for bugs and specific change requests.