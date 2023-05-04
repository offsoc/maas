package ethernet

/*
	Copyright 2023 Canonical Ltd.  This software is licensed under the
	GNU Affero General Public License version 3 (see the file LICENSE).
*/

import (
	"encoding/binary"
	"errors"
	"io"
	"net"
)

const (
	minEthernetLen = 14
)

const (
	// EthernetTypeLLC is a special ethernet type, if found the frame is truncated
	EthernetTypeLLC uint16 = 0
	// EthernetTypeIPv4 is the ethernet type for a frame containing an IPv4 packet
	EthernetTypeIPv4 uint16 = 0x0800
	// EthernetTypeARP is the ethernet type for a frame containing an ARP packet
	EthernetTypeARP uint16 = 0x0806
	// EthernetTypeIPv6 is the ethernet type for a frame containing an IPv6 packet
	EthernetTypeIPv6 uint16 = 0x86dd
	// EthernetTypeVLAN is the ethernet type for a frame containing a VLAN tag,
	// the VLAN tag bytes will indicate the actual type of packet the frame contains
	EthernetTypeVLAN uint16 = 0x8100

	// NonStdLenEthernetTypes is a magic number to find any non-standard types
	// and mark them as EthernetTypeLLC
	NonStdLenEthernetTypes uint16 = 0x600
)

var (
	// ErrNotVLAN is an error returned when calling EthernetFrame.ExtractVLAN
	// if the frame is not of type EthernetTypeVLAN
	ErrNotVLAN = errors.New("ethernet frame not of type VLAN")
	// ErrMalformedVLAN is an error returned when parsing a VLAN tag
	// that is malformed
	ErrMalformedVLAN = errors.New("VLAN tag is malformed")
	// ErrMalformedFrame is an error returned when parsing an ethernet frame
	// that is malformed
	ErrMalformedFrame = errors.New("malformed ethernet frame")
)

// VLAN represents a VLAN tag within an ethernet frame
type VLAN struct {
	Priority     uint8
	DropEligible bool
	ID           uint16
	EthernetType uint16
}

// UnmarshalBinary will take the ethernet frame's payload
// and extract a VLAN tag if one is present
func (v *VLAN) UnmarshalBinary(buf []byte) error {
	if len(buf) < 4 {
		return ErrMalformedVLAN
	}

	// extract the first 3 bits
	v.Priority = (buf[0] & 0xe0) >> 5
	// extract the next bit and turn it into a bool
	v.DropEligible = buf[0]&0x10 != 0
	// extract the next 12 bits for an ID
	v.ID = binary.BigEndian.Uint16(buf[:2]) & 0x0fff
	// last 2 bytes are ethernet type
	v.EthernetType = binary.BigEndian.Uint16(buf[2:])

	return nil
}

// EthernetFrame represents an ethernet frame
type EthernetFrame struct {
	SrcMAC       net.HardwareAddr
	DstMAC       net.HardwareAddr
	Payload      []byte
	Len          uint16
	EthernetType uint16
}

// ExtractARPPacket will extract an ARP packet from the ethernet frame's
// payload
func (e *EthernetFrame) ExtractARPPacket() (*ARPPacket, error) {
	var buf []byte
	if e.EthernetType == EthernetTypeVLAN {
		buf = e.Payload[4:]
	} else {
		buf = e.Payload
	}

	a := &ARPPacket{}

	err := a.UnmarshalBinary(buf)
	if err != nil {
		return nil, err
	}

	return a, nil
}

// ExtractVLAN will extract the VLAN tag from the ethernet frame's
// payload if one is present and return ErrNotVLAN if not
func (e *EthernetFrame) ExtractVLAN() (*VLAN, error) {
	if e.EthernetType != EthernetTypeVLAN {
		return nil, ErrNotVLAN
	}

	v := &VLAN{}

	err := v.UnmarshalBinary(e.Payload[0:4])
	if err != nil {
		return nil, err
	}

	return v, nil
}

// UnmarshalBinary parses ethernet frame bytes into an EthernetFrame
func (e *EthernetFrame) UnmarshalBinary(buf []byte) error {
	if len(buf) < minEthernetLen {
		if len(buf) == 0 {
			return io.ErrUnexpectedEOF
		}

		return ErrMalformedFrame
	}

	e.DstMAC = buf[0:6]
	e.SrcMAC = buf[6:12]
	e.EthernetType = binary.BigEndian.Uint16(buf[12:14])
	e.Payload = buf[14:]

	if e.EthernetType < NonStdLenEthernetTypes {
		// see IEEE 802.3, non-standard ethernet may contain padding
		// this calculation is used to truncate the payload to the length
		// specified for that ethernet type
		e.Len = e.EthernetType
		e.EthernetType = EthernetTypeLLC

		cmp := len(e.Payload) - int(e.Len)
		if cmp < 0 {
			return ErrMalformedFrame
		} else if cmp > 0 {
			e.Payload = e.Payload[:len(e.Payload)-cmp]
		}
	}

	return nil
}
