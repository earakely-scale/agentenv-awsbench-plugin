Quarterly access review (SEC-230 series) finding: vl-rm-admin-role (instance profile of rm-admin-01) holds dynamodb:RestoreTableToPointInTime and RestoreTableFromBackup on table/routemap-state*. Restores are rare and should go through a human SRE session.

Change: add explicit deny statement `DenyRestoreSEC205` for dynamodb:Restore* to vl-rm-admin-role. Consequence: `rmctl state restore` stops working; restores run with the SRE-OnCall permission set (aws CLI or console).

Exception recorded: the PlatformEng permission set keeps standing prod write access for platform maintenance ("exception: on-call needs", owner Kwame Asante). Re-review in Q1 2027.
