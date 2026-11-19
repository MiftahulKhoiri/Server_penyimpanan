package id.serverpenyimpanan.android

import id.serverpenyimpanan.android.data.api.ApiJson
import id.serverpenyimpanan.android.data.api.MountPointRequest
import id.serverpenyimpanan.android.data.api.SetupActionOut
import id.serverpenyimpanan.android.data.api.StorageSetupOut
import id.serverpenyimpanan.android.ui.admin.isValidMountPoint
import id.serverpenyimpanan.android.ui.admin.partitionSubtitle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DiskSetupTest {
    @Test
    fun mountPointValidation_matchesServerRules() {
        listOf("/mnt/data", "/media/backup_2", "/srv/hdd.1", "/mnt/A-1").forEach { assertTrue(it, isValidMountPoint(it)) }
        listOf("/etc", "/mnt", "/mnt/", "/mnt/a/b", "/mnt/../etc", "mnt/data", "/mnt/-x", "/mnt/a b", "/home/pi")
            .forEach { assertFalse(it, isValidMountPoint(it)) }
    }

    @Test
    fun storageSetup_decodesServerResponse() {
        val json = """
            {"helper_available": true, "helper_error": null, "restart_pending": false,
             "roles": [
               {"role": "ssd", "mount_point": "/mnt/ssd",
                "paths": {"PRIMARY_STORAGE_PATH": "/mnt/ssd/storage", "DATABASE_PATH": "/mnt/ssd/database/server.db"},
                "mounted": true, "device": "/dev/sda1", "uuid": "aaaa-1", "fstype": "ext4", "label": "ssd",
                "model": "Samsung", "managed": false, "fstab_uuid": null, "total_bytes": 1000, "free_bytes": 600,
                "can_mount": false, "can_unmount": false},
               {"role": "hdd", "mount_point": "/mnt/hdd", "paths": {"BACKUP_STORAGE_PATH": "/mnt/hdd/backup"},
                "mounted": false, "device": null, "uuid": null, "fstype": null, "label": null, "model": null,
                "managed": false, "fstab_uuid": null, "total_bytes": null, "free_bytes": null,
                "can_mount": true, "can_unmount": false}],
             "partitions": [
               {"path": "/dev/sdb1", "name": "sdb1", "disk": "sdb", "size_bytes": 2000398934016, "fstype": "ext4",
                "uuid": "bbbb-2", "label": "backup", "model": "WD Elements", "transport": "usb", "removable": false,
                "mountpoint": null, "system": false, "usable": true, "reason": null, "reason_text": null, "role": null},
               {"path": "/dev/mmcblk0p2", "name": "mmcblk0p2", "disk": "mmcblk0", "size_bytes": 3000, "fstype": "ext4",
                "uuid": "1111", "label": null, "model": null, "transport": null, "removable": false, "mountpoint": "/",
                "system": true, "usable": false, "reason": "system_disk", "reason_text": "Disk sistem", "role": null}],
             "job": {"id": 3, "op": "set_mount_point", "role": "ssd", "status": "running", "detail": {"from": "/mnt/ssd"},
                     "steps": ["Menghentikan server sementara"], "message": null, "error_code": null,
                     "started_at": "2026-11-19T02:00:00Z", "finished_at": null}}
        """.trimIndent()
        val setup = ApiJson.decodeFromString(StorageSetupOut.serializer(), json)
        assertTrue(setup.helperAvailable)
        val ssd = setup.roles.first { it.isSsd }
        assertEquals("/mnt/ssd/database/server.db", ssd.paths["DATABASE_PATH"])
        assertFalse(ssd.canMount)
        assertTrue(setup.roles.first { it.role == "hdd" }.canMount)
        val hdd = setup.partitions.first()
        assertTrue(hdd.usable)
        assertEquals(2000398934016, hdd.sizeBytes)
        assertEquals("/dev/sdb1 · ext4 · WD Elements · USB", partitionSubtitle(hdd))
        assertEquals("system_disk", setup.partitions[1].reason)
        assertTrue(setup.job!!.isRunning)
    }

    @Test
    fun helperMissing_decodesWithDefaults() {
        val setup = ApiJson.decodeFromString(
            StorageSetupOut.serializer(),
            """{"helper_available": false, "helper_error": "Helper belum terpasang", "restart_pending": false}""",
        )
        assertFalse(setup.helperAvailable)
        assertTrue(setup.roles.isEmpty())
        assertNull(setup.job)
    }

    @Test
    fun mountPointRequest_usesSnakeCase() {
        assertEquals("""{"mount_point":"/mnt/data"}""", ApiJson.encodeToString(MountPointRequest.serializer(), MountPointRequest("/mnt/data")))
        val action = ApiJson.decodeFromString(
            SetupActionOut.serializer(),
            """{"detail": "x", "restarting": true, "job": {"id": 1, "op": "set_mount_point", "role": "hdd", "status": "running"}}""",
        )
        assertTrue(action.restarting)
    }
}
